"""
PC-98 Disk Image Mounter — Main GUI Application (wxPython)

Mount PC-98 floppy/HDD disk images as browsable directories (Linux/WSL)
or native drive letters (Windows).

Requires: wxPython  (pip install wxPython)
On Windows mounting uses the built-in `subst` command (or VHD with admin).
On Linux/WSL files are extracted to a directory you can browse directly.

Three mount modes:
  - FAT:     files and folders from the FAT12/16 filesystem
  - Flat:    entire raw image as a single DISK.IMG file
  - Sectors: each sector as an individual SECTOR_NNNN.BIN file

Also includes a built-in hex viewer for sector-level inspection.

Usage:
    python pc98mount.py
    python pc98mount.py image.hdm
    python pc98mount.py image.d88 -d P --mode flat        # Windows
    python pc98mount.py image.d88 -n game --mode flat     # Linux/WSL
"""

import sys
import os
from pathlib import Path
import logging

import wx
import wx.dataview as dv

from disk_image import open_image
from fat_fs import FATFilesystem
from hex_viewer import HexViewerPanel
from mount_backend import (
    MountManager, is_windows, is_wsl, _cached_is_wsl,
    open_in_file_manager,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("pc98mount")

def _build_wildcard():
    """
    Build a file-dialog wildcard string.
    On Linux/GTK, file dialogs are case-sensitive, so the filter
    includes both lower- and upper-case variants — but the display
    label only shows lowercase to keep it clean.
    On Windows the dialog is already case-insensitive.
    """
    groups = [
        ("PC-98 Disk Images",
         ["d88", "d68", "d77", "hdm", "tfd", "fdi", "hdi", "img", "ima"]),
        ("D88 Images",  ["d88", "d68", "d77"]),
        ("HDM Images",  ["hdm", "tfd"]),
        ("FDI Images",  ["fdi"]),
        ("HDI Images",  ["hdi"]),
        ("Raw Images",  ["img", "ima"]),
        ("All Files",   ["*"]),
    ]
    parts = []
    for label, exts in groups:
        if exts == ["*"]:
            parts.append("All Files (*.*)|*.*")
            continue
        display_pats = [f"*.{e}" for e in exts]
        if is_windows():
            filter_pats = display_pats
        else:
            filter_pats = []
            for e in exts:
                filter_pats.append(f"*.{e}")
                if e.upper() != e:
                    filter_pats.append(f"*.{e.upper()}")
        display = ";".join(display_pats)
        filt = ";".join(filter_pats)
        parts.append(f"{label} ({display})|{filt}")
    return "|".join(parts)


IMAGE_WILDCARD = _build_wildcard()


# =============================================================================
# Drive-letter helpers (Windows) / mount-name helpers (Linux)
# =============================================================================

def _drive_letter_in_use(letter):
    if not is_windows():
        return False
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        res = ctypes.windll.kernel32.QueryDosDeviceW(
            f"{letter}:", buf, len(buf))
        if res != 0:
            return True
        err = ctypes.windll.kernel32.GetLastError()
        return err != 2
    except Exception:
        return False


def get_available_drive_letters():
    if not is_windows():
        return []
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        bitmask = 0
    used = set()
    for i in range(26):
        if bitmask & (1 << i):
            used.add(chr(ord('A') + i))
    for i in range(26):
        letter = chr(ord('A') + i)
        if _drive_letter_in_use(letter):
            used.add(letter)
    preferred = [chr(c) for c in range(ord('P'), ord('Z') + 1)]
    others = [chr(c) for c in range(ord('D'), ord('P'))]
    return [l for l in preferred + others if l not in used]


_LINUX_MOUNT_SLOTS = [f"slot{i}" for i in range(1, 11)]


# =============================================================================
# Data model
# =============================================================================

class ImageInfo:
    """Holds state for a loaded disk image."""
    def __init__(self, path, disk, fs, label):
        self.path = path
        self.disk = disk
        self.fs = fs
        self.label = label
        self.mount_id = None
        self.mount_mode = None


# =============================================================================
# Main Frame
# =============================================================================

class PC98MountFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="PC-98 Disk Image Mounter",
                         size=(960, 660))
        self.SetMinSize((720, 500))

        self.images = []
        self.mount_mgr = MountManager()
        self._tree_paths = {}

        self._build_ui()
        self._update_mount_targets()
        self.Centre()

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- Toolbar ---
        tb_sizer = wx.BoxSizer(wx.HORIZONTAL)

        btn_add = wx.Button(panel, label="Add Image\u2026")
        btn_add.Bind(wx.EVT_BUTTON, self._on_add_image)
        tb_sizer.Add(btn_add, 0, wx.RIGHT, 4)

        btn_remove = wx.Button(panel, label="Remove")
        btn_remove.Bind(wx.EVT_BUTTON, self._on_remove_image)
        tb_sizer.Add(btn_remove, 0, wx.RIGHT, 4)

        tb_sizer.AddSpacer(16)

        lbl_target = "Drive:" if is_windows() else "Slot:"
        tb_sizer.Add(wx.StaticText(panel, label=lbl_target),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.target_combo = wx.ComboBox(
            panel, size=(100 if is_windows() else 110, -1),
            style=wx.CB_READONLY)
        tb_sizer.Add(self.target_combo, 0, wx.RIGHT, 4)

        # Refresh button — Windows only (refreshes drive letters)
        if is_windows():
            btn_refresh = wx.Button(panel, label="\u21BB", size=(32, -1))
            btn_refresh.SetToolTip("Refresh available drive letters")
            btn_refresh.Bind(wx.EVT_BUTTON, self._on_refresh_targets)
            tb_sizer.Add(btn_refresh, 0, wx.RIGHT, 8)

        tb_sizer.Add(wx.StaticText(panel, label="Mode:"),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.mode_combo = wx.ComboBox(
            panel, choices=["fat", "flat", "sectors"],
            value="fat", size=(90, -1), style=wx.CB_READONLY)
        tb_sizer.Add(self.mode_combo, 0, wx.RIGHT, 8)

        btn_mount = wx.Button(panel, label="Mount")
        btn_mount.Bind(wx.EVT_BUTTON, self._on_mount)
        tb_sizer.Add(btn_mount, 0, wx.RIGHT, 4)

        btn_unmount = wx.Button(panel, label="Unmount")
        btn_unmount.Bind(wx.EVT_BUTTON, self._on_unmount)
        tb_sizer.Add(btn_unmount, 0, wx.RIGHT, 4)

        tb_sizer.AddSpacer(16)

        browse_label = ("Open in Explorer" if is_windows()
                        else "Open in File Manager")
        btn_browse = wx.Button(panel, label=browse_label)
        btn_browse.Bind(wx.EVT_BUTTON, self._on_open_file_manager)
        tb_sizer.Add(btn_browse, 0)

        main_sizer.Add(tb_sizer, 0, wx.EXPAND | wx.ALL, 6)

        # --- Splitter: image list | notebook ---
        splitter = wx.SplitterWindow(
            panel, style=wx.SP_LIVE_UPDATE)

        # Left: image list
        left_panel = wx.Panel(splitter)
        left_sizer = wx.BoxSizer(wx.VERTICAL)
        left_sizer.Add(wx.StaticText(left_panel, label="Disk Images"),
                        0, wx.BOTTOM, 4)
        self.image_listbox = wx.ListBox(left_panel)
        self.image_listbox.Bind(wx.EVT_LISTBOX, self._on_image_select)
        left_sizer.Add(self.image_listbox, 1, wx.EXPAND)
        left_panel.SetSizer(left_sizer)

        # Right: notebook
        right_panel = wx.Panel(splitter)
        right_sizer = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(right_panel)

        self._build_files_page()
        self._build_hex_page()
        self._build_info_page()

        right_sizer.Add(self.notebook, 1, wx.EXPAND)
        right_panel.SetSizer(right_sizer)

        splitter.SetMinimumPaneSize(120)
        splitter.SplitVertically(left_panel, right_panel, 180)
        main_sizer.Add(splitter, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        # --- Status bar ---
        self.status_bar = self.CreateStatusBar()
        self._set_status("Ready. Add a PC-98 disk image to begin.")

        panel.SetSizer(main_sizer)
        panel.Layout()

        self.Bind(wx.EVT_CLOSE, self._on_close)

    def _build_files_page(self):
        page = wx.Panel(self.notebook)
        self.notebook.AddPage(page, "  Files  ")
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.info_label = wx.StaticText(page, label="No image loaded")
        sizer.Add(self.info_label, 0, wx.EXPAND | wx.BOTTOM, 4)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_ext = wx.Button(page, label="Extract File\u2026")
        btn_ext.Bind(wx.EVT_BUTTON, self._on_extract_file)
        btn_ext_all = wx.Button(page, label="Extract All\u2026")
        btn_ext_all.Bind(wx.EVT_BUTTON, self._on_extract_all)
        btn_sizer.Add(btn_ext, 0, wx.RIGHT, 4)
        btn_sizer.Add(btn_ext_all, 0)
        sizer.Add(btn_sizer, 0, wx.BOTTOM, 4)

        # File tree
        self.file_tree = dv.TreeListCtrl(
            page, style=dv.TL_SINGLE)
        self.file_tree.AppendColumn("Name", width=280)
        self.file_tree.AppendColumn("Size", width=90,
                                     align=wx.ALIGN_RIGHT)
        self.file_tree.AppendColumn("Modified", width=140)
        self.file_tree.AppendColumn("Attr", width=50,
                                     align=wx.ALIGN_CENTER)
        sizer.Add(self.file_tree, 1, wx.EXPAND)

        page.SetSizer(sizer)

    def _build_hex_page(self):
        page = wx.Panel(self.notebook)
        self.notebook.AddPage(page, "  Hex Viewer  ")
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.hex_viewer = HexViewerPanel(page)
        sizer.Add(self.hex_viewer, 1, wx.EXPAND)
        page.SetSizer(sizer)

    def _build_info_page(self):
        page = wx.Panel(self.notebook)
        self.notebook.AddPage(page, "  Image Info  ")
        sizer = wx.BoxSizer(wx.VERTICAL)

        mono = wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL,
                        wx.FONTWEIGHT_NORMAL)
        self.detail_text = wx.TextCtrl(
            page,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.detail_text.SetFont(mono)
        sizer.Add(self.detail_text, 1, wx.EXPAND)

        page.SetSizer(sizer)

    # ── Helpers ──────────────────────────────────────────────────────

    def _set_status(self, msg):
        self.status_bar.SetStatusText(msg)

    def _selected_image(self):
        idx = self.image_listbox.GetSelection()
        if idx == wx.NOT_FOUND:
            return None
        return self.images[idx]

    def _mount_display(self, info):
        if not info.mount_id:
            return None
        if is_windows():
            return f"{info.mount_id}:"
        return info.mount_id

    @staticmethod
    def _format_size(size):
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.1f} MB"

    def _update_mount_targets(self):
        self.target_combo.Clear()
        if is_windows():
            available = get_available_drive_letters()
            for l in available:
                self.target_combo.Append(f"{l}:")
            if available:
                self.target_combo.SetSelection(0)
        else:
            used = {
                info.mount_id for info in self.images
                if info.mount_id
                and self.mount_mgr.is_mounted(info.mount_id)
            }
            available = [s for s in _LINUX_MOUNT_SLOTS if s not in used]
            for s in available:
                self.target_combo.Append(s)
            if available:
                self.target_combo.SetSelection(0)

    def _on_refresh_targets(self, event):
        """Refresh the drive-letter list (Windows only)."""
        self._update_mount_targets()
        self._set_status("Drive letters refreshed.")

    # ── Image management ─────────────────────────────────────────────

    def _on_add_image(self, event):
        dlg = wx.FileDialog(
            self, "Select PC-98 Disk Image(s)",
            wildcard=IMAGE_WILDCARD,
            style=wx.FD_OPEN | wx.FD_MULTIPLE)
        dlg.CentreOnParent()
        if dlg.ShowModal() == wx.ID_OK:
            for path in dlg.GetPaths():
                self._load_image(path)
        dlg.Destroy()

    def _load_image(self, path):
        self._set_status(f"Loading {Path(path).name}\u2026")
        try:
            disk = open_image(path)
            fs = None
            try:
                fs = FATFilesystem(disk)
            except Exception as e:
                log.info(f"No FAT filesystem: {e}")

            label = ((fs.volume_label if fs and fs.volume_label else None)
                     or disk.label or Path(path).stem)

            info = ImageInfo(path, disk, fs, label)
            self.images.append(info)

            self.image_listbox.Append(Path(path).name)
            self.image_listbox.SetSelection(len(self.images) - 1)
            self._on_image_select(None)

            total_kb = disk.total_sectors * disk.sector_size // 1024
            fat_str = f"FAT{fs.fat_type}" if fs else "No FAT"
            file_count = (
                sum(1 for _, e in fs.walk() if not e.is_directory)
                if fs else 0
            )
            status = (
                f"Loaded {Path(path).name}: {fat_str}, "
                f"{disk.total_sectors} sectors \u00d7 "
                f"{disk.sector_size}B = {total_kb} KB"
            )
            if fs:
                if file_count > 0:
                    status += f", {file_count} files found"
                else:
                    status += (" \u2014 WARNING: FAT parsed but "
                               "0 files found!")
            else:
                status += " (raw access only)"
            self._set_status(status)
        except Exception as e:
            wx.MessageBox(f"Could not load:\n{path}\n\n{e}",
                          "Error", wx.OK | wx.ICON_ERROR)
            self._set_status(f"Error: {e}")

    def _on_remove_image(self, event):
        idx = self.image_listbox.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        info = self.images[idx]
        if info.mount_id and self.mount_mgr.is_mounted(info.mount_id):
            self.mount_mgr.unmount(info.mount_id)
        self.images.pop(idx)
        self.image_listbox.Delete(idx)
        self._clear_tree()
        self._set_status("Image removed.")

    # ── Selection / display ──────────────────────────────────────────

    def _on_image_select(self, event):
        info = self._selected_image()
        if not info:
            return
        self._populate_tree(info)
        self.hex_viewer.set_disk(info.disk)
        self._populate_detail(info)

    def _clear_tree(self):
        self.file_tree.DeleteAllItems()
        self._tree_paths.clear()
        self.info_label.SetLabel("No image loaded")

    def _populate_tree(self, info):
        self._clear_tree()

        if info.fs is None:
            self.info_label.SetLabel(
                f"{info.label} \u2014 No FAT filesystem. "
                f"Use Hex Viewer or mount in flat/sector mode.")
            return

        mounted = ""
        if info.mount_id and self.mount_mgr.is_mounted(info.mount_id):
            disp = self._mount_display(info)
            mounted = f"  [mounted at {disp}]"

        total_kb = info.disk.total_sectors * info.disk.sector_size // 1024
        self.info_label.SetLabel(
            f"{info.label} \u2014 FAT{info.fs.fat_type}, "
            f"{info.disk.sector_size}B sectors, {total_kb} KB{mounted}")

        root = self.file_tree.GetRootItem()
        self._add_dir_entries(info, info.fs.root, root, '')

    def _add_dir_entries(self, info, dir_entry, parent_item, path_prefix):
        entries = sorted(
            dir_entry.children.values(),
            key=lambda e: (not e.is_directory, e.display_name.upper()))

        for entry in entries:
            if entry.name in ('.', '..'):
                continue
            name = entry.display_name
            full_path = f"{path_prefix}/{name}"

            icon = "\U0001F4C1 " if entry.is_directory else "\U0001F4C4 "
            size_str = ("<DIR>" if entry.is_directory
                        else self._format_size(entry.size))
            date_str = entry.datetime.strftime("%Y-%m-%d %H:%M")

            attr_parts = []
            if entry.attr & 0x01: attr_parts.append('R')
            if entry.attr & 0x02: attr_parts.append('H')
            if entry.attr & 0x04: attr_parts.append('S')
            if entry.attr & 0x20: attr_parts.append('A')

            item = self.file_tree.AppendItem(
                parent_item, f"{icon}{name}")
            self.file_tree.SetItemText(item, 1, size_str)
            self.file_tree.SetItemText(item, 2, date_str)
            self.file_tree.SetItemText(item, 3, ''.join(attr_parts))

            self.file_tree.SetItemData(item, (info, full_path, entry))

            if entry.is_directory:
                self._add_dir_entries(info, entry, item, full_path)

    def _populate_detail(self, info):
        lines = [
            f"File:            {info.path}",
            f"File Size:       {os.path.getsize(info.path):,} bytes",
            f"Image Type:      {info.disk.__class__.__name__}",
            f"Label:           {info.label}",
            f"Sector Size:     {info.disk.sector_size} bytes",
            f"Total Sectors:   {info.disk.total_sectors}",
            f"Total Size:      "
            f"{info.disk.total_sectors * info.disk.sector_size:,} bytes",
            "",
        ]

        if info.fs:
            fs = info.fs
            lines += [
                "\u2500\u2500 FAT Filesystem \u2500" * 3,
                f"FAT Type:        FAT{fs.fat_type}",
                f"Bytes/Sector:    {fs.bytes_per_sector}",
                f"Sects/Cluster:   {fs.sectors_per_cluster}",
                f"Reserved Sects:  {fs.reserved_sectors}",
                f"Number of FATs:  {fs.num_fats}",
                f"FAT Size:        {fs.fat_size_16} sectors",
                f"Root Entries:    {fs.root_entry_count}",
                f"Media Desc:      0x{fs.media_descriptor:02X}",
                f"Total Clusters:  {fs.total_clusters}",
                f"Data Start:      sector {fs.first_data_sector}",
                "",
            ]
            file_count = sum(
                1 for _, e in fs.walk() if not e.is_directory)
            dir_count = sum(
                1 for _, e in fs.walk() if e.is_directory)
            total_size = sum(
                e.size for _, e in fs.walk() if not e.is_directory)
            lines += [
                f"Files:           {file_count}",
                f"Directories:     {dir_count}",
                f"Data Used:       {total_size:,} bytes",
            ]
        else:
            lines += [
                "No FAT filesystem detected.",
                "Use Hex Viewer or mount in flat/sector mode.",
            ]

        lines += ["", "\u2500\u2500 Mount Status \u2500" * 3]
        if info.mount_id and self.mount_mgr.is_mounted(info.mount_id):
            mount_obj = self.mount_mgr.get_mount(info.mount_id)
            loc = mount_obj.mount_point if mount_obj else info.mount_id
            lines.append(f"Mounted:         {info.mount_mode} at {loc}")
        else:
            lines.append("Mounted:         No")

        lines += ["", "\u2500\u2500 Mount Strategy \u2500" * 3]
        lines.append(self.mount_mgr.get_strategy_info())

        self.detail_text.SetValue('\n'.join(lines))

    # ── Mount / Unmount ──────────────────────────────────────────────

    def _on_mount(self, event):
        info = self._selected_image()
        if not info:
            wx.MessageBox("Select a disk image first.",
                          "No Image", wx.OK | wx.ICON_INFORMATION)
            return

        if info.mount_id and self.mount_mgr.is_mounted(info.mount_id):
            disp = self._mount_display(info)
            wx.MessageBox(f"Already mounted at {disp}",
                          "Mounted", wx.OK | wx.ICON_INFORMATION)
            return

        target = self.target_combo.GetValue().rstrip(':')
        if not target:
            msg = ("Select a drive letter." if is_windows()
                   else "Select a mount slot.")
            wx.MessageBox(msg, "No Target", wx.OK | wx.ICON_INFORMATION)
            return

        mode = self.mode_combo.GetValue()
        if mode == "fat" and info.fs is None:
            wx.MessageBox(
                "No FAT filesystem on this image.\n"
                "Use 'flat' or 'sectors' mode for raw access.",
                "No FAT", wx.OK | wx.ICON_WARNING)
            return

        # --- Show busy message while mounting ---
        self._set_status(
            f"Mounting {Path(info.path).name} at {target} ({mode})\u2026")

        busy = wx.BusyInfo("Please wait, mounting in progress\u2026")
        wx.GetApp().Yield()  # let the busy overlay paint

        try:
            image_size = info.disk.total_sectors * info.disk.sector_size
            self.mount_mgr.mount(
                target, mode,
                disk_image=info.disk,
                fat_fs=info.fs,
                image_size_bytes=image_size,
            )
            info.mount_id = target
            info.mount_mode = mode

            # Update listbox
            idx = self.image_listbox.GetSelection()
            if idx != wx.NOT_FOUND:
                disp = self._mount_display(info)
                self.image_listbox.SetString(
                    idx, f"[{disp}] {Path(info.path).name}")
                self.image_listbox.SetSelection(idx)

            self._populate_tree(info)
            self._populate_detail(info)
            self._update_mount_targets()

            mode_desc = {
                "fat": "FAT filesystem (files/folders)",
                "flat": "raw flat file (DISK.IMG)",
                "sectors": (f"individual sectors "
                            f"({info.disk.total_sectors} files)"),
            }
            strategy = self.mount_mgr.strategy or ""
            via = f" via {strategy.upper()}" if strategy else ""
            mount_obj = self.mount_mgr.get_mount(target)
            location = (mount_obj.mount_point if mount_obj
                        else target)
            self._set_status(
                f"Mounted at {location}{via} \u2014 "
                f"{mode_desc.get(mode, mode)}")

            # Check for empty FAT extraction
            if mode == "fat" and mount_obj:
                count = getattr(mount_obj, '_extract_count', -1)
                errors = getattr(mount_obj, '_extract_errors', 0)
                if count == 0:
                    wx.MessageBox(
                        f"The FAT parser found 0 files on this "
                        f"image.\n"
                        f"({errors} extraction errors)\n\n"
                        f"This disk likely has no standard FAT "
                        f"filesystem.\n"
                        f"Try 'flat' or 'sectors' mode, or use the "
                        f"Hex Viewer.",
                        "No Files Extracted",
                        wx.OK | wx.ICON_WARNING)
                elif count > 0:
                    extra = (f" ({errors} errors)" if errors else "")
                    self._set_status(
                        f"Mounted at {location}{via} \u2014 "
                        f"{count} files extracted{extra}")

        except Exception as e:
            wx.MessageBox(f"Failed to mount:\n\n{e}",
                          "Mount Error", wx.OK | wx.ICON_ERROR)
            self._set_status(f"Mount failed: {e}")
        finally:
            del busy  # dismiss the busy overlay

    def _on_unmount(self, event):
        info = self._selected_image()
        if not info:
            return
        if (not info.mount_id
                or not self.mount_mgr.is_mounted(info.mount_id)):
            wx.MessageBox("This image is not mounted.",
                          "Not Mounted", wx.OK | wx.ICON_INFORMATION)
            return

        mid = info.mount_id
        self.mount_mgr.unmount(mid)
        info.mount_id = None
        info.mount_mode = None

        idx = self.image_listbox.GetSelection()
        if idx != wx.NOT_FOUND:
            self.image_listbox.SetString(idx, Path(info.path).name)
            self.image_listbox.SetSelection(idx)

        self._populate_tree(info)
        self._populate_detail(info)
        self._update_mount_targets()
        self._set_status(f"Unmounted {mid}")

    def _on_open_file_manager(self, event):
        info = self._selected_image()
        if not info or not info.mount_id:
            wx.MessageBox("Mount an image first.",
                          "Not Mounted", wx.OK | wx.ICON_INFORMATION)
            return
        mount_obj = self.mount_mgr.get_mount(info.mount_id)
        if mount_obj:
            open_in_file_manager(mount_obj.mount_point)
        else:
            self._set_status("Mount not found")

    # ── File extraction ──────────────────────────────────────────────

    def _on_extract_file(self, event):
        item = self.file_tree.GetSelection()
        if not item.IsOk():
            wx.MessageBox("Select a file in the tree.",
                          "No Selection", wx.OK | wx.ICON_INFORMATION)
            return

        data = self.file_tree.GetItemData(item)
        if data is None:
            return
        info, path, entry = data

        if entry.is_directory:
            dlg = wx.DirDialog(
                self,
                f"Extract '{entry.display_name}' to\u2026",
                style=wx.DD_DEFAULT_STYLE)
            dlg.CentreOnParent()
            if dlg.ShowModal() == wx.ID_OK:
                self._extract_dir(info, entry, dlg.GetPath(),
                                  entry.display_name)
            dlg.Destroy()
        else:
            dlg = wx.FileDialog(
                self, "Extract File",
                defaultFile=entry.display_name,
                style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
            dlg.CentreOnParent()
            if dlg.ShowModal() == wx.ID_OK:
                try:
                    file_data = info.fs.read_file(entry)
                    with open(dlg.GetPath(), 'wb') as f:
                        f.write(file_data)
                    self._set_status(
                        f"Extracted {entry.display_name} "
                        f"({len(file_data)} bytes)")
                except Exception as e:
                    wx.MessageBox(f"Extract failed:\n{e}",
                                  "Error", wx.OK | wx.ICON_ERROR)
            dlg.Destroy()

    def _on_extract_all(self, event):
        info = self._selected_image()
        if not info or not info.fs:
            wx.MessageBox("No filesystem to extract.",
                          "No FAT", wx.OK | wx.ICON_INFORMATION)
            return

        dlg = wx.DirDialog(self, "Extract all files to\u2026",
                            style=wx.DD_DEFAULT_STYLE)
        dlg.CentreOnParent()
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return
        dest = dlg.GetPath()
        dlg.Destroy()

        count = 0
        for fpath, entry in info.fs.walk():
            full = os.path.join(dest, fpath.lstrip('/'))
            if entry.is_directory:
                os.makedirs(full, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                try:
                    with open(full, 'wb') as f:
                        f.write(info.fs.read_file(entry))
                    count += 1
                except Exception as e:
                    log.warning(f"Failed: {fpath}: {e}")

        self._set_status(f"Extracted {count} files to {dest}")
        wx.MessageBox(f"Extracted {count} files to:\n{dest}",
                      "Done", wx.OK | wx.ICON_INFORMATION)

    def _extract_dir(self, info, dir_entry, dest_base, dir_name):
        dest = os.path.join(dest_base, dir_name)
        os.makedirs(dest, exist_ok=True)
        count = 0
        for name, entry in dir_entry.children.items():
            if entry.name in ('.', '..'):
                continue
            if entry.is_directory:
                self._extract_dir(info, entry, dest,
                                  entry.display_name)
            else:
                try:
                    with open(
                        os.path.join(dest, entry.display_name), 'wb'
                    ) as f:
                        f.write(info.fs.read_file(entry))
                    count += 1
                except Exception as e:
                    log.warning(f"Failed: {entry.display_name}: {e}")
        self._set_status(f"Extracted {count} files to {dest}")

    # ── Cleanup ──────────────────────────────────────────────────────

    def _on_close(self, event):
        self.mount_mgr.unmount_all()
        self.Destroy()


# =============================================================================
# Application entry point
# =============================================================================

class PC98MountApp(wx.App):
    def OnInit(self):
        self.frame = PC98MountFrame()
        self.frame.Show()
        return True

    def load_and_mount(self, images, mount_id, mode):
        """Called after MainLoop starts via CallAfter."""
        for path in images:
            if os.path.isfile(path):
                self.frame._load_image(os.path.abspath(path))

        if mount_id and len(self.frame.images) == 1:
            if is_windows():
                self.frame.target_combo.SetValue(f"{mount_id}:")
            else:
                self.frame.target_combo.SetValue(mount_id)
            self.frame.mode_combo.SetValue(mode)
            wx.CallLater(200, self.frame._on_mount, None)


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="PC-98 Disk Image Mounter")
    parser.add_argument('images', nargs='*',
                        help='Disk image files to open')

    if is_windows():
        parser.add_argument('-d', '--drive',
                            help='Drive letter to mount (e.g. P)')
    else:
        parser.add_argument('-n', '--name', default=None,
                            help='Mount slot name (default: auto)')
        parser.add_argument('-d', '--drive', default=None,
                            help='Alias for --name (Linux/WSL compat)')

    parser.add_argument('-m', '--mode',
                        choices=['fat', 'flat', 'sectors'],
                        default='fat', help='Mount mode')
    args = parser.parse_args()

    mount_id = None
    if is_windows():
        if args.drive:
            mount_id = args.drive.upper()
    else:
        mount_id = (getattr(args, 'name', None)
                    or getattr(args, 'drive', None))

    app = PC98MountApp()

    if args.images or mount_id:
        wx.CallAfter(app.load_and_mount,
                     args.images, mount_id, args.mode)

    app.MainLoop()


if __name__ == '__main__':
    main()