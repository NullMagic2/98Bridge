"""
Mount backends for PC-98 disk images.

Supports Windows and Linux (including WSL).

Windows strategies (tried in order):
  1. VHD (preferred) — creates a virtual hard disk of the correct size,
     formats it, copies files, and mounts as a drive letter.
     Shows correct disk size in Explorer. Requires admin privileges.
  2. subst (fallback) — extracts to a temp directory and uses `subst`
     to assign a drive letter. No admin needed, but Explorer shows the
     host drive's size instead of the image's.

Linux / WSL strategy:
  3. Directory mount — extracts to a directory under a configurable base
     path (default: ~/.local/share/pc98mount/mounts).

All strategies are read-only from the image's perspective.
"""

import os
import sys
import shutil
import subprocess
import tempfile
import time
import logging
from pathlib import Path

log = logging.getLogger("pc98mount.mount")


# =============================================================================
# Platform detection
# =============================================================================

def is_windows():
    return sys.platform == 'win32'


def is_wsl():
    """Detect Windows Subsystem for Linux."""
    if is_windows():
        return False
    try:
        with open('/proc/version', 'r') as f:
            return 'microsoft' in f.read().lower()
    except (OSError, IOError):
        return False


_IS_WSL = None


def _cached_is_wsl():
    global _IS_WSL
    if _IS_WSL is None:
        _IS_WSL = is_wsl()
    return _IS_WSL


# =============================================================================
# Helpers
# =============================================================================

def _sanitize_filename(name):
    """Remove or replace characters invalid in filenames."""
    invalid = '<>:"/\\|?*'
    result = ''.join(c if c not in invalid else '_' for c in name)
    result = result.rstrip('. ')
    return result if result else '_unnamed_'


def _extract_fat_to_dir(fat_fs, dir_entry, dest_path, counters):
    """Recursively extract a FAT directory tree to a real directory."""
    for name, entry in dir_entry.children.items():
        if entry.name in ('.', '..'):
            continue

        safe_name = _sanitize_filename(entry.display_name)
        target = os.path.join(dest_path, safe_name)

        if entry.is_directory:
            log.info(f"  DIR:  {safe_name}/")
            os.makedirs(target, exist_ok=True)
            _extract_fat_to_dir(fat_fs, entry, target, counters)
        else:
            try:
                data = fat_fs.read_file(entry)
                with open(target, 'wb') as f:
                    f.write(data)
                counters['files'] += 1
                log.info(f"  FILE: {safe_name} ({len(data)} bytes)")
            except Exception as e:
                counters['errors'] += 1
                log.error(f"  FAIL: {safe_name}: {e}")


def _write_flat_to_dir(disk_image, dest_path, filename="DISK.IMG"):
    """Write the entire raw image as a single file."""
    out_path = os.path.join(dest_path, filename)
    data = disk_image.read_sectors(0, disk_image.total_sectors)
    with open(out_path, 'wb') as f:
        f.write(data)
    log.info(f"Wrote {len(data):,} bytes to {out_path}")


def _write_sectors_to_dir(disk_image, dest_path):
    """Write each sector as an individual file."""
    width = len(str(disk_image.total_sectors - 1))
    for i in range(disk_image.total_sectors):
        filename = f"SECTOR_{i:0{width}d}.BIN"
        data = disk_image.read_sector(i)
        with open(os.path.join(dest_path, filename), 'wb') as f:
            f.write(data)
    log.info(f"Wrote {disk_image.total_sectors} sector files to {dest_path}")


def _is_admin():
    """Check if we're running with admin/root privileges."""
    if is_windows():
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    else:
        return os.geteuid() == 0


def _run_diskpart(script_text):
    """
    Run a diskpart script (Windows only). Returns (success, output).
    Requires admin privileges.
    """
    script_path = os.path.join(tempfile.gettempdir(), 'pc98_diskpart.txt')
    with open(script_path, 'w') as f:
        f.write(script_text)

    try:
        result = subprocess.run(
            ['diskpart', '/s', script_path],
            capture_output=True, text=True, timeout=30
        )
        output = result.stdout + result.stderr
        log.info(f"diskpart output:\n{output}")
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, "diskpart timed out"
    except FileNotFoundError:
        return False, "diskpart not found"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def _run_elevated(command, args):
    """
    Run a command with UAC elevation (Windows only). Returns (success, output).
    Uses ShellExecuteExW to trigger the UAC prompt.
    """
    if not is_windows():
        return False, "Not on Windows"

    import ctypes

    batch_path = os.path.join(tempfile.gettempdir(), 'pc98_elevated.bat')
    output_path = os.path.join(tempfile.gettempdir(), 'pc98_elevated_out.txt')
    done_path = os.path.join(tempfile.gettempdir(), 'pc98_elevated_done.txt')

    for p in (output_path, done_path):
        try:
            os.unlink(p)
        except OSError:
            pass

    with open(batch_path, 'w') as f:
        f.write(f'@echo off\n')
        f.write(f'{command} {args} > "{output_path}" 2>&1\n')
        f.write(f'echo DONE > "{done_path}"\n')

    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", "cmd.exe",
            f'/c "{batch_path}"',
            None, 0  # SW_HIDE
        )

        if ret <= 32:
            return False, f"ShellExecute failed (code {ret})"

        for _ in range(60):
            time.sleep(0.5)
            if os.path.exists(done_path):
                break
        else:
            return False, "Elevated process timed out"

        output = ""
        if os.path.exists(output_path):
            with open(output_path, 'r', errors='replace') as f:
                output = f.read()

        return True, output

    finally:
        for p in (batch_path, output_path, done_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def open_in_file_manager(path):
    """Open a path in the platform's file manager."""
    if is_windows():
        os.startfile(path)
    elif _cached_is_wsl():
        # WSL: use Windows Explorer via interop
        try:
            result = subprocess.run(
                ['wslpath', '-w', path],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                win_path = result.stdout.strip()
                subprocess.Popen(['explorer.exe', win_path])
            else:
                subprocess.Popen(['explorer.exe', path])
        except FileNotFoundError:
            # wslpath or explorer.exe not available, fall back
            subprocess.Popen(['xdg-open', path])
    else:
        # Native Linux / macOS
        try:
            subprocess.Popen(['xdg-open', path])
        except FileNotFoundError:
            try:
                subprocess.Popen(['open', path])   # macOS
            except FileNotFoundError:
                log.error("No file manager command found (tried xdg-open, open)")


# =============================================================================
# Strategy 1: VHD Mount (Windows only, correct disk size, needs admin)
# =============================================================================

class VHDMount:
    """
    Creates a VHD (Virtual Hard Disk), formats it to the correct size,
    copies files, and assigns a drive letter. Explorer shows the real size.
    Windows only.
    """

    def __init__(self, drive_letter):
        self.drive_letter = drive_letter.upper().rstrip(':')
        self._vhd_path = None
        self._temp_dir = None
        self._mounted = False
        self._extract_count = 0
        self._extract_errors = 0

    @property
    def mount_point(self):
        return f"{self.drive_letter}:"

    @property
    def is_mounted(self):
        return self._mounted

    def mount_fat(self, fat_fs, image_size_bytes):
        """Mount a FAT filesystem via VHD."""
        self._temp_dir = tempfile.mkdtemp(prefix="pc98vhd_")
        staging = os.path.join(self._temp_dir, "staging")
        os.makedirs(staging)

        counters = {'files': 0, 'errors': 0}
        _extract_fat_to_dir(fat_fs, fat_fs.root, staging, counters)
        self._extract_count = counters['files']
        self._extract_errors = counters['errors']

        log.info(f"Extracted {counters['files']} files, {counters['errors']} errors")
        self._create_and_mount_vhd(image_size_bytes, staging)

    def mount_flat(self, disk_image):
        """Mount raw image as single file via VHD."""
        self._temp_dir = tempfile.mkdtemp(prefix="pc98vhd_")
        staging = os.path.join(self._temp_dir, "staging")
        os.makedirs(staging)

        _write_flat_to_dir(disk_image, staging)
        size = disk_image.total_sectors * disk_image.sector_size
        self._create_and_mount_vhd(size, staging)

    def mount_sectors(self, disk_image):
        """Mount sectors as individual files via VHD."""
        self._temp_dir = tempfile.mkdtemp(prefix="pc98vhd_")
        staging = os.path.join(self._temp_dir, "staging")
        os.makedirs(staging)

        _write_sectors_to_dir(disk_image, staging)
        size = disk_image.total_sectors * disk_image.sector_size
        self._create_and_mount_vhd(size, staging)

    def _create_and_mount_vhd(self, image_size_bytes, staging_dir):
        """Create a VHD, format it, copy files from staging, assign drive letter."""
        vhd_size_mb = max(3, (image_size_bytes // (1024 * 1024)) + 2)
        self._vhd_path = os.path.join(self._temp_dir, "pc98disk.vhd")

        log.info(f"Creating {vhd_size_mb}MB VHD at {self._vhd_path}")

        create_script = (
            f'create vdisk file="{self._vhd_path}" maximum={vhd_size_mb} type=fixed\n'
            f'select vdisk file="{self._vhd_path}"\n'
            f'attach vdisk\n'
            f'create partition primary\n'
            f'format fs=fat32 quick label="PC98"\n'
            f'assign letter={self.drive_letter}\n'
        )

        if _is_admin():
            ok, output = _run_diskpart(create_script)
        else:
            script_path = os.path.join(self._temp_dir, 'create.txt')
            with open(script_path, 'w') as f:
                f.write(create_script)
            ok, output = _run_elevated('diskpart', f'/s "{script_path}"')

        if not ok:
            raise RuntimeError(f"Failed to create VHD:\n{output}")

        drive_path = f"{self.drive_letter}:\\"
        for _ in range(20):
            if os.path.exists(drive_path):
                break
            time.sleep(0.3)
        else:
            raise RuntimeError(
                f"Drive {self.drive_letter}: did not appear after VHD mount"
            )

        log.info(f"Copying files to {drive_path}")
        self._copy_tree(staging_dir, drive_path)

        try:
            for root, dirs, files in os.walk(drive_path):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        os.chmod(fpath, 0o444)
                    except OSError:
                        pass
        except Exception:
            pass

        self._mounted = True
        log.info(f"VHD mounted at {self.drive_letter}:")

    def _copy_tree(self, src, dst):
        """Copy all files from src to dst."""
        for item in os.listdir(src):
            s = os.path.join(src, item)
            d = os.path.join(dst, item)
            if os.path.isdir(s):
                os.makedirs(d, exist_ok=True)
                self._copy_tree(s, d)
            else:
                shutil.copy2(s, d)

    def unmount(self):
        """Detach the VHD and clean up."""
        if not self._mounted and not self._vhd_path:
            return

        if self._vhd_path and os.path.exists(self._vhd_path):
            detach_script = (
                f'select vdisk file="{self._vhd_path}"\n'
                f'detach vdisk\n'
            )
            if _is_admin():
                _run_diskpart(detach_script)
            else:
                script_path = os.path.join(
                    tempfile.gettempdir(), 'pc98_detach.txt'
                )
                with open(script_path, 'w') as f:
                    f.write(detach_script)
                _run_elevated('diskpart', f'/s "{script_path}"')
                time.sleep(1)

        self._mounted = False

        if self._temp_dir and os.path.exists(self._temp_dir):
            time.sleep(0.5)
            try:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
                log.info(f"Cleaned up {self._temp_dir}")
            except Exception as e:
                log.warning(f"Cleanup failed: {e}")

        self._temp_dir = None
        self._vhd_path = None


# =============================================================================
# Strategy 2: Extract + subst (Windows, zero dependencies, no admin)
# =============================================================================

class SubstMount:
    """
    Extracts image contents to a temp directory and uses
    Windows `subst` to map a drive letter to that directory.
    No admin required, but Explorer shows host drive's space.
    Windows only.
    """

    def __init__(self, drive_letter):
        self.drive_letter = drive_letter.upper().rstrip(':')
        self._temp_dir = None
        self._mounted = False
        self._extract_count = 0
        self._extract_errors = 0

    @property
    def mount_point(self):
        return f"{self.drive_letter}:"

    @property
    def is_mounted(self):
        return self._mounted

    def mount_fat(self, fat_fs):
        self._temp_dir = tempfile.mkdtemp(prefix="pc98_")
        counters = {'files': 0, 'errors': 0}
        _extract_fat_to_dir(fat_fs, fat_fs.root, self._temp_dir, counters)
        self._extract_count = counters['files']
        self._extract_errors = counters['errors']
        log.info(
            f"Extracted {counters['files']} files, "
            f"{counters['errors']} errors to {self._temp_dir}"
        )
        if counters['files'] == 0:
            log.warning("No files extracted!")
        self._subst_mount()

    def mount_flat(self, disk_image):
        self._temp_dir = tempfile.mkdtemp(prefix="pc98raw_")
        _write_flat_to_dir(disk_image, self._temp_dir)
        self._subst_mount()

    def mount_sectors(self, disk_image):
        self._temp_dir = tempfile.mkdtemp(prefix="pc98sec_")
        _write_sectors_to_dir(disk_image, self._temp_dir)
        self._subst_mount()

    def _subst_mount(self):
        try:
            subprocess.run(
                ['subst', f'{self.drive_letter}:', '/D'],
                capture_output=True, check=False
            )
            result = subprocess.run(
                ['subst', f'{self.drive_letter}:', self._temp_dir],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"subst failed: {result.stderr.strip() or result.stdout.strip()}"
                )
            self._mounted = True
            log.info(f"Mounted {self._temp_dir} at {self.drive_letter}: via subst")
        except FileNotFoundError:
            raise RuntimeError("'subst' command not found")

    def unmount(self):
        if not self._mounted:
            return
        try:
            subprocess.run(
                ['subst', f'{self.drive_letter}:', '/D'],
                capture_output=True, text=True
            )
        except Exception as e:
            log.warning(f"subst /D failed: {e}")

        self._mounted = False
        if self._temp_dir and os.path.exists(self._temp_dir):
            try:
                shutil.rmtree(self._temp_dir, ignore_errors=True)
                log.info(f"Cleaned up {self._temp_dir}")
            except Exception as e:
                log.warning(f"Cleanup failed: {e}")
        self._temp_dir = None


# =============================================================================
# Strategy 3: Directory mount (Linux / WSL / any platform)
# =============================================================================

class DirectoryMount:
    """
    Extracts image contents into a directory and exposes that path as the
    mount point.  Works on any platform — no admin, no special OS features.

    The mount point is a real directory, not a drive letter.
    """

    DEFAULT_BASE = os.path.expanduser("~/.local/share/pc98mount/mounts")

    def __init__(self, mount_path):
        """
        mount_path: the directory that will contain the extracted files.
        Created on mount, removed on unmount.
        """
        self._mount_path = os.path.abspath(mount_path)
        self._mounted = False
        self._extract_count = 0
        self._extract_errors = 0

    @property
    def mount_point(self):
        return self._mount_path

    @property
    def is_mounted(self):
        return self._mounted

    def mount_fat(self, fat_fs):
        self._ensure_dir()
        counters = {'files': 0, 'errors': 0}
        _extract_fat_to_dir(fat_fs, fat_fs.root, self._mount_path, counters)
        self._extract_count = counters['files']
        self._extract_errors = counters['errors']
        log.info(
            f"Extracted {counters['files']} files, "
            f"{counters['errors']} errors to {self._mount_path}"
        )
        if counters['files'] == 0:
            log.warning("No files extracted!")
        self._mounted = True

    def mount_flat(self, disk_image):
        self._ensure_dir()
        _write_flat_to_dir(disk_image, self._mount_path)
        self._mounted = True

    def mount_sectors(self, disk_image):
        self._ensure_dir()
        _write_sectors_to_dir(disk_image, self._mount_path)
        self._mounted = True

    def unmount(self):
        if not self._mounted:
            return
        self._mounted = False
        if os.path.exists(self._mount_path):
            try:
                shutil.rmtree(self._mount_path, ignore_errors=True)
                log.info(f"Cleaned up {self._mount_path}")
            except Exception as e:
                log.warning(f"Cleanup failed: {e}")

    def _ensure_dir(self):
        """Create the mount directory, cleaning it first if it already exists."""
        if os.path.exists(self._mount_path):
            shutil.rmtree(self._mount_path, ignore_errors=True)
        os.makedirs(self._mount_path, exist_ok=True)

    @classmethod
    def default_base(cls):
        """Return the default base directory for new mounts."""
        return cls.DEFAULT_BASE


# =============================================================================
# Unified mount interface
# =============================================================================

class MountManager:
    """
    High-level mount interface.

    On Windows: tries VHD first, falls back to subst.
    On Linux/WSL: uses DirectoryMount (extract to a named directory).

    Mount identifiers:
      - Windows: drive letter string like "P"
      - Linux:   a short name (becomes a subdirectory under mount_base)
                 or a full absolute path
    """

    STRATEGY_VHD = "vhd"
    STRATEGY_SUBST = "subst"
    STRATEGY_DIRECTORY = "directory"

    def __init__(self, mount_base=None):
        self._mounts = {}   # key -> mount object
        self._strategy = None
        self._mount_base = mount_base or DirectoryMount.default_base()

    @property
    def strategy(self):
        return self._strategy

    @property
    def mount_base(self):
        return self._mount_base

    @mount_base.setter
    def mount_base(self, value):
        self._mount_base = value

    def get_strategy_info(self):
        if self._strategy == self.STRATEGY_VHD:
            return (
                "Using VHD (Virtual Hard Disk).\n"
                "Explorer shows the correct image size.\n"
                "Files extracted from image; original is not modified."
            )
        elif self._strategy == self.STRATEGY_SUBST:
            return (
                "Using Windows `subst` (no admin).\n"
                "Explorer shows host drive's size (cosmetic only).\n"
                "Run as Administrator for correct size display via VHD."
            )
        elif self._strategy == self.STRATEGY_DIRECTORY:
            wsl_tag = " (WSL detected)" if _cached_is_wsl() else ""
            return (
                f"Using directory extraction{wsl_tag}.\n"
                f"Mount base: {self._mount_base}\n"
                "Original image is not modified."
            )
        return "No mount active."

    # -- key normalisation ----------------------------------------------------

    def _mount_key(self, identifier):
        """Normalise a mount identifier to a dict key."""
        if is_windows():
            return identifier.upper().rstrip(':')
        else:
            if not os.path.isabs(identifier):
                return os.path.abspath(
                    os.path.join(self._mount_base, identifier)
                )
            return os.path.abspath(identifier)

    # -- public API -----------------------------------------------------------

    def mount(self, mount_id, mode, disk_image=None, fat_fs=None,
              image_size_bytes=0, prefer_vhd=True):
        """
        Mount an image.

        mount_id:
          - Windows: drive letter (e.g. "P")
          - Linux/WSL: a short name that becomes a subdirectory under
            mount_base, or a full absolute path.
        """
        key = self._mount_key(mount_id)
        if key in self._mounts:
            raise RuntimeError(f"{mount_id} is already mounted")

        if image_size_bytes == 0 and disk_image:
            image_size_bytes = disk_image.total_sectors * disk_image.sector_size

        if is_windows():
            return self._mount_windows(
                key, mode, disk_image, fat_fs, image_size_bytes, prefer_vhd
            )
        else:
            return self._mount_linux(key, mount_id, mode, disk_image, fat_fs)

    def unmount(self, mount_id):
        key = self._mount_key(mount_id)
        mount = self._mounts.pop(key, None)
        if mount:
            mount.unmount()
        return mount is not None

    def unmount_all(self):
        for key in list(self._mounts.keys()):
            mount = self._mounts.pop(key, None)
            if mount:
                mount.unmount()

    def is_mounted(self, mount_id):
        key = self._mount_key(mount_id)
        mount = self._mounts.get(key)
        return mount is not None and mount.is_mounted

    def get_mount(self, mount_id):
        key = self._mount_key(mount_id)
        return self._mounts.get(key)

    # -- internal -------------------------------------------------------------

    def _mount_windows(self, drive, mode, disk_image, fat_fs,
                       image_size_bytes, prefer_vhd):
        """Windows: VHD first, then subst fallback."""
        if prefer_vhd:
            try:
                mount = self._try_vhd_mount(
                    drive, mode, disk_image, fat_fs, image_size_bytes
                )
                self._mounts[drive] = mount
                self._strategy = self.STRATEGY_VHD
                return mount
            except Exception as e:
                log.warning(f"VHD mount failed, falling back to subst: {e}")

        mount = SubstMount(drive)
        self._do_mount(mount, mode, disk_image, fat_fs)
        self._mounts[drive] = mount
        self._strategy = self.STRATEGY_SUBST
        return mount

    def _mount_linux(self, key, mount_id, mode, disk_image, fat_fs):
        """Linux/WSL: extract into a directory."""
        if not os.path.isabs(mount_id):
            mount_path = os.path.join(self._mount_base, mount_id)
        else:
            mount_path = mount_id

        mount = DirectoryMount(mount_path)
        self._do_mount(mount, mode, disk_image, fat_fs)
        self._mounts[key] = mount
        self._strategy = self.STRATEGY_DIRECTORY
        return mount

    @staticmethod
    def _do_mount(mount, mode, disk_image, fat_fs):
        """Run the appropriate extraction for *mount*."""
        if mode == 'fat':
            if fat_fs is None:
                raise ValueError("FAT filesystem required for fat mode")
            mount.mount_fat(fat_fs)
        elif mode == 'flat':
            if disk_image is None:
                raise ValueError("Disk image required for flat mode")
            mount.mount_flat(disk_image)
        elif mode == 'sectors':
            if disk_image is None:
                raise ValueError("Disk image required for sectors mode")
            mount.mount_sectors(disk_image)
        else:
            raise ValueError(f"Unknown mode: {mode}")

    def _try_vhd_mount(self, drive, mode, disk_image, fat_fs,
                       image_size_bytes):
        """Attempt to mount via VHD (Windows only)."""
        mount = VHDMount(drive)
        if mode == 'fat':
            if fat_fs is None:
                raise ValueError("FAT filesystem required")
            mount.mount_fat(fat_fs, image_size_bytes)
        elif mode == 'flat':
            if disk_image is None:
                raise ValueError("Disk image required")
            mount.mount_flat(disk_image)
        elif mode == 'sectors':
            if disk_image is None:
                raise ValueError("Disk image required")
            mount.mount_sectors(disk_image)
        return mount
