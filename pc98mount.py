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

The **Update** button writes any modifications made through the file
manager back into the disk image (overwrite or save-as).

Usage:
    python pc98mount.py
    python pc98mount.py image.hdm
    python pc98mount.py image.d88 -d P --mode flat        # Windows
    python pc98mount.py image.d88 -n game --mode flat     # Linux/WSL
"""

import os
import threading
from pathlib import Path
import logging

import wx
import wx.dataview as dv

# ── Plugin infrastructure — must be imported before the domain
#    modules so the registry is available when they self-register.
import registry                                         # noqa: F401
import plugin_loader

from disk_image import open_image, create_blank_image, BLANK_GEOMETRIES, BLANK_FORMATS
from disk_image import (
    RawImage, FDIImage, HDIImage, D88Image,
    _write_fdi, _write_hdi, _write_d88,
)
from hex_viewer import HexViewerPanel
from mount_backend import (
    MountManager, is_windows,
    open_in_file_manager,
)
from plugin_manager import PluginManagerDialog

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("pc98mount")

# Load all plugins (core + user) from the plugins/ directory.
plugin_loader.load_plugins()

if not registry.get_image_formats():
    log.warning(
        "No image formats registered!  Make sure plugins/core/ "
        "contains pc98_formats.py and fat_filesystem.py."
    )


def _build_wildcard():
    """Build a file-dialog wildcard string from the plugin registry.

    Includes one "All Supported" group, one group per registered
    format, and a final "All Files" catch-all.  If no formats are
    registered (shouldn't happen) it falls back to ``*.*``.
    """
    formats = registry.get_image_formats()

    # Collect every known extension for the "all supported" group.
    all_exts: list[str] = []
    seen: set[str] = set()
    for fmt in formats:
        for ext in fmt.extensions:
            bare = ext.lstrip('.')
            if bare not in seen:
                seen.add(bare)
                all_exts.append(bare)

    def _make_filter(label, exts):
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
        return f"{label} ({display})|{filt}"

    parts: list[str] = []
    if all_exts:
        parts.append(_make_filter("All Supported Images", all_exts))

    for fmt in formats:
        bare_exts = [e.lstrip('.') for e in fmt.extensions]
        parts.append(_make_filter(fmt.group_label, bare_exts))

    parts.append("All Files (*.*)|*.*")
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
# Blank Image Dialog
# =============================================================================

# Map format names to default file extensions
_FORMAT_EXTENSIONS = {
    "HDM":          ".hdm",
    "D88":          ".d88",
    "FDI":          ".fdi",
    "HDI":          ".hdi",
    "RAW (.img)":   ".img",
}


class BlankImageDialog(wx.Dialog):
    """Dialog for creating a new blank disk image."""

    # Which geometries are valid for which container formats.
    _FLOPPY_FORMATS = {"HDM", "D88", "FDI", "RAW (.img)"}
    _HDD_FORMATS    = {"HDI"}

    _FLOPPY_GEOMS = [k for k, v in BLANK_GEOMETRIES.items()
                     if v is not None and k.startswith("PC-98")]
    _HDD_GEOMS    = [k for k, v in BLANK_GEOMETRIES.items()
                     if v is not None and k.startswith("HDD")]

    # Default geometry to select for each format category.
    _DEFAULT_FLOPPY = "PC-98 2HD (1.2 MB)"
    _DEFAULT_HDD    = "HDD 40 MB"

    def __init__(self, parent):
        super().__init__(parent, title="Create Blank Disk Image",
                         size=(500, 340),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.SetMinSize((460, 300))
        self._path = None
        self._build_ui()
        self._on_format_change(None)   # sync geometry list to initial format

    def _build_ui(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Format ────────────────────────────────────────────────
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Format:", size=(90, -1)),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.fmt_combo = wx.ComboBox(
            self, choices=BLANK_FORMATS,
            value=BLANK_FORMATS[0], style=wx.CB_READONLY)
        self.fmt_combo.Bind(wx.EVT_COMBOBOX, self._on_format_change)
        row.Add(self.fmt_combo, 1)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)

        # ── Geometry preset ───────────────────────────────────────
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Geometry:", size=(90, -1)),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.geom_combo = wx.ComboBox(
            self, choices=[], style=wx.CB_READONLY)
        self.geom_combo.Bind(wx.EVT_COMBOBOX, self._on_geom_change)
        row.Add(self.geom_combo, 1)
        sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Custom CHS controls inside a panel (hidden by default) ──
        self.custom_panel = wx.Panel(self)
        cp_sizer = wx.BoxSizer(wx.VERTICAL)

        box = wx.StaticBox(self.custom_panel, label="Custom Geometry")
        box_sizer = wx.StaticBoxSizer(box, wx.VERTICAL)

        grid = wx.FlexGridSizer(cols=4, hgap=8, vgap=6)
        grid.AddGrowableCol(1)
        grid.AddGrowableCol(3)

        grid.Add(wx.StaticText(self.custom_panel, label="Cylinders:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_cyls = wx.SpinCtrl(
            self.custom_panel, min=1, max=16383, initial=615,
            size=(90, -1))
        self.spin_cyls.Bind(wx.EVT_SPINCTRL, self._on_custom_change)
        grid.Add(self.spin_cyls, 0)

        grid.Add(wx.StaticText(self.custom_panel, label="Heads:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_heads = wx.SpinCtrl(
            self.custom_panel, min=1, max=255, initial=4,
            size=(90, -1))
        self.spin_heads.Bind(wx.EVT_SPINCTRL, self._on_custom_change)
        grid.Add(self.spin_heads, 0)

        grid.Add(wx.StaticText(self.custom_panel, label="Sectors/Track:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_spt = wx.SpinCtrl(
            self.custom_panel, min=1, max=255, initial=17,
            size=(90, -1))
        self.spin_spt.Bind(wx.EVT_SPINCTRL, self._on_custom_change)
        grid.Add(self.spin_spt, 0)

        grid.Add(wx.StaticText(self.custom_panel, label="Sector Size:"),
                 0, wx.ALIGN_CENTER_VERTICAL)
        self.combo_secsize = wx.ComboBox(
            self.custom_panel, choices=["256", "512", "1024"],
            value="512", style=wx.CB_READONLY, size=(90, -1))
        self.combo_secsize.Bind(wx.EVT_COMBOBOX, self._on_custom_change)
        grid.Add(self.combo_secsize, 0)

        box_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 4)
        cp_sizer.Add(box_sizer, 0, wx.EXPAND)
        self.custom_panel.SetSizer(cp_sizer)

        sizer.Add(self.custom_panel, 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.custom_panel.Hide()

        # ── Geometry detail label ─────────────────────────────────
        self.geom_detail = wx.StaticText(self, label="")
        self.geom_detail.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(self.geom_detail, 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Format with FAT checkbox (floppy only) ───────────────
        self.fat_check = wx.CheckBox(
            self, label="Format with empty FAT filesystem")
        self.fat_check.SetValue(True)
        self.fat_check.SetToolTip(
            "Write a valid FAT12/16 boot sector and empty FAT table\n"
            "so the image is ready to use immediately.")
        sizer.Add(self.fat_check, 0,
                  wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── HDI hint label (shown only for hard disk) ────────────
        self.hdi_hint = wx.StaticText(
            self,
            label="\u26a0  Hard disk images are created blank. You must\n"
                  "    initialise and format them from within DOS\n"
                  "    using FORMAT.EXE (select \u201c\u56fa\u5b9a"
                  "\u30c7\u30a3\u30b9\u30af\u201d).")
        self.hdi_hint.SetForegroundColour(wx.Colour(160, 120, 0))
        sizer.Add(self.hdi_hint, 0,
                  wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.hdi_hint.Hide()

        # ── Save location ─────────────────────────────────────────
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Save to:", size=(90, -1)),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.path_ctrl = wx.TextCtrl(self, value="", style=wx.TE_READONLY)
        row.Add(self.path_ctrl, 1, wx.RIGHT, 4)
        btn_browse = wx.Button(self, label="Browse\u2026")
        btn_browse.Bind(wx.EVT_BUTTON, self._on_browse)
        row.Add(btn_browse, 0)
        sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Buttons ───────────────────────────────────────────────
        btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindowById(wx.ID_OK).SetLabel("Create")
        self.FindWindowById(wx.ID_OK).Bind(wx.EVT_BUTTON, self._on_ok)
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(sizer)
        self.Layout()
        self.Fit()

    # ── Helpers ───────────────────────────────────────────────────

    def _is_hdd_format(self):
        return self.fmt_combo.GetValue() in self._HDD_FORMATS

    def _sync_geometry_list(self):
        """Repopulate the geometry combo to show only geometries
        compatible with the selected container format."""
        if self._is_hdd_format():
            geoms = self._HDD_GEOMS + ["Custom"]
            default = self._DEFAULT_HDD
        else:
            geoms = self._FLOPPY_GEOMS + ["Custom"]
            default = self._DEFAULT_FLOPPY

        self.geom_combo.Set(geoms)
        if default in geoms:
            self.geom_combo.SetValue(default)
        else:
            self.geom_combo.SetSelection(0)

    # ── Event handlers ────────────────────────────────────────────

    def _on_format_change(self, event):
        """Called when the Format combo changes — sync geometry list
        and show/hide the FAT checkbox and HDI hint."""
        self._sync_geometry_list()

        is_hdd = self._is_hdd_format()
        # HDI images can't be pre-formatted (needs DISKINIT in DOS).
        self.fat_check.Show(not is_hdd)
        self.hdi_hint.Show(is_hdd)

        # Sync custom panel visibility.
        is_custom = self.geom_combo.GetValue() == "Custom"
        self.custom_panel.Show(is_custom)

        self._update_geom_detail()
        self.Layout()
        self.Fit()

    def _on_geom_change(self, event):
        is_custom = self.geom_combo.GetValue() == "Custom"
        self.custom_panel.Show(is_custom)
        self.Layout()
        self.Fit()
        self._update_geom_detail()

    def _on_custom_change(self, event):
        self._update_geom_detail()

    def _update_geom_detail(self):
        name = self.geom_combo.GetValue()
        if name == "Custom":
            cyls, heads, spt, ss = self._get_custom_chs()
        elif name in BLANK_GEOMETRIES and BLANK_GEOMETRIES[name]:
            cyls, heads, spt, ss = BLANK_GEOMETRIES[name]
        else:
            self.geom_detail.SetLabel("")
            return

        total = cyls * heads * spt * ss
        if total >= 1024 * 1024:
            size_str = f"{total / (1024 * 1024):.1f} MB"
        else:
            size_str = f"{total / 1024:.0f} KB"
        self.geom_detail.SetLabel(
            f"  {cyls} cyl \u00d7 {heads} heads \u00d7 {spt} spt "
            f"\u00d7 {ss} B/sector  =  {total:,} bytes ({size_str})")

    def _get_custom_chs(self):
        """Read the CHS values from the spin controls."""
        return (
            self.spin_cyls.GetValue(),
            self.spin_heads.GetValue(),
            self.spin_spt.GetValue(),
            int(self.combo_secsize.GetValue()),
        )

    def _on_browse(self, event):
        fmt = self.fmt_combo.GetValue()
        ext = _FORMAT_EXTENSIONS.get(fmt, ".img")
        wildcard = (f"{fmt} files (*{ext})|*{ext}|"
                    f"All Files (*.*)|*.*")
        dlg = wx.FileDialog(
            self, "Save Blank Image As",
            defaultFile=f"blank{ext}",
            wildcard=wildcard,
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        dlg.CentreOnParent()
        if dlg.ShowModal() == wx.ID_OK:
            self._path = dlg.GetPath()
            self.path_ctrl.SetValue(self._path)
        dlg.Destroy()

    def _on_ok(self, event):
        if not self._path:
            wx.MessageBox(
                "Please choose a location to save the image.",
                "No Path", wx.OK | wx.ICON_WARNING)
            return

        # Validate custom geometry
        geom = self.get_geometry()
        if isinstance(geom, tuple):
            cyls, heads, spt, ss = geom
            total = cyls * heads * spt * ss
            if total == 0:
                wx.MessageBox(
                    "Image size would be 0 bytes.\n"
                    "Check your geometry values.",
                    "Invalid Geometry", wx.OK | wx.ICON_WARNING)
                return
            if total > 2 * 1024 * 1024 * 1024:
                wx.MessageBox(
                    f"Image would be {total / (1024**3):.1f} GB.\n"
                    f"FAT16 supports up to ~2 GB.",
                    "Too Large", wx.OK | wx.ICON_WARNING)
                return

        self.EndModal(wx.ID_OK)

    # ── Accessors ─────────────────────────────────────────────────

    def get_format(self):
        val = self.fmt_combo.GetValue()
        if val.startswith("RAW"):
            return "RAW"
        return val

    def get_geometry(self):
        """Return a geometry name (str) or a (C,H,S,secsize) tuple."""
        name = self.geom_combo.GetValue()
        if name == "Custom":
            return self._get_custom_chs()
        return name

    def get_format_fat(self):
        # HDI images cannot be pre-formatted — they need DISKINIT in DOS.
        if self._is_hdd_format():
            return False
        return self.fat_check.GetValue()

    def get_path(self):
        return self._path


# =============================================================================
# Convert Image Dialog
# =============================================================================

# Known floppy geometries by raw size: (cyls, heads, spt, sector_size)
_RAW_FLOPPY_GEOMETRIES = {
    1261568: (77,  2,  8, 1024),  # PC-98 2HD 1.2 MB
    1228800: (77,  2,  8, 1024),  # PC-98 2HD alternate
    737280:  (80,  2,  9,  512),  # 2DD 720 KB
    655360:  (80,  2,  8,  512),  # 2DD 640 KB
    1474560: (80,  2, 18,  512),  # 1.44 MB
}

_CONVERT_TARGETS = ["FDI", "HDI", "D88", "HDM / RAW"]


class ConvertDialog(wx.Dialog):
    """Dialog for converting a disk image to a different container format."""

    def __init__(self, parent, source_path=None, source_disk=None):
        super().__init__(parent, title="Convert Disk Image",
                         size=(520, 280),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.SetMinSize((460, 260))
        self._source_path = source_path or ""
        self._source_disk = source_disk
        self._save_path = None
        self._build_ui()
        self._update_info()

    def _build_ui(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ── Source file ──────────────────────────────────────────
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Source:", size=(80, -1)),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.source_ctrl = wx.TextCtrl(
            self, value=self._source_path, style=wx.TE_READONLY)
        row.Add(self.source_ctrl, 1, wx.RIGHT, 4)
        btn_src = wx.Button(self, label="Browse\u2026")
        btn_src.Bind(wx.EVT_BUTTON, self._on_browse_source)
        row.Add(btn_src, 0)
        sizer.Add(row, 0, wx.EXPAND | wx.ALL, 8)

        # ── Source info ──────────────────────────────────────────
        self.info_label = wx.StaticText(self, label="")
        self.info_label.SetForegroundColour(wx.Colour(100, 100, 100))
        sizer.Add(self.info_label, 0,
                  wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Target format ────────────────────────────────────────
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Convert to:", size=(80, -1)),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.fmt_combo = wx.ComboBox(
            self, choices=_CONVERT_TARGETS,
            value=_CONVERT_TARGETS[0], style=wx.CB_READONLY)
        row.Add(self.fmt_combo, 1)
        sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Save location ────────────────────────────────────────
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, label="Save to:", size=(80, -1)),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.save_ctrl = wx.TextCtrl(
            self, value="", style=wx.TE_READONLY)
        row.Add(self.save_ctrl, 1, wx.RIGHT, 4)
        btn_dst = wx.Button(self, label="Browse\u2026")
        btn_dst.Bind(wx.EVT_BUTTON, self._on_browse_dest)
        row.Add(btn_dst, 0)
        sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # ── Buttons ──────────────────────────────────────────────
        btn_sizer = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        self.FindWindowById(wx.ID_OK).SetLabel("Convert")
        self.FindWindowById(wx.ID_OK).Bind(wx.EVT_BUTTON, self._on_ok)
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 8)

        self.SetSizer(sizer)
        self.Layout()
        self.Fit()

    # ── Helpers ───────────────────────────────────────────────────

    def _load_source(self, path):
        """Try to open *path* with the plugin registry."""
        try:
            self._source_disk = open_image(path)
            self._source_path = path
            self.source_ctrl.SetValue(path)
            self._update_info()
            return True
        except Exception as exc:
            wx.MessageBox(
                f"Cannot open image:\n{path}\n\n{exc}",
                "Open Error", wx.OK | wx.ICON_ERROR)
            return False

    def _update_info(self):
        disk = self._source_disk
        if not disk:
            self.info_label.SetLabel("  No source image loaded.")
            return
        raw_bytes = disk.total_sectors * disk.sector_size
        if raw_bytes >= 1024 * 1024:
            size_str = f"{raw_bytes / (1024 * 1024):.1f} MB"
        else:
            size_str = f"{raw_bytes / 1024:.0f} KB"

        geom = self._detect_geometry()
        if geom:
            c, h, s, ss = geom
            self.info_label.SetLabel(
                f"  {disk.label}  \u2014  {c}C/{h}H/{s}S "
                f"\u00d7 {ss}B  =  {raw_bytes:,} bytes ({size_str})")
        else:
            self.info_label.SetLabel(
                f"  {disk.label}  \u2014  {disk.total_sectors:,} sectors "
                f"\u00d7 {disk.sector_size}B  =  "
                f"{raw_bytes:,} bytes ({size_str})")

    def _detect_geometry(self):
        """Return (cyls, heads, spt, sector_size) or None."""
        disk = self._source_disk
        if disk is None:
            return None

        # FDI / HDI / D88 already store geometry.
        for attr_set in (
            ('_cyls',),
            # HDIImage stores _spt and _heads but not _cyls;
            # we can derive cyls from total_sectors.
        ):
            pass

        spt = getattr(disk, '_spt', 0)
        heads = getattr(disk, '_heads', 0)
        ss = disk.sector_size

        if spt and heads:
            cyls = disk.total_sectors // (spt * heads) or 1
            return (cyls, heads, spt, ss)

        # FDIImage: geometry is parsed from header.
        # Try to extract from the label "FDI (77C/2H/8S)" etc.
        label = getattr(disk, '_label', '') or ''
        import re
        m = re.search(r'(\d+)C/(\d+)H/(\d+)S', label)
        if m:
            return (int(m.group(1)), int(m.group(2)),
                    int(m.group(3)), ss)

        # Raw images: try known floppy sizes.
        raw_bytes = disk.total_sectors * ss
        if raw_bytes in _RAW_FLOPPY_GEOMETRIES:
            return _RAW_FLOPPY_GEOMETRIES[raw_bytes]

        # Hard disk guess: 512-byte sectors, 17 spt, 8 heads.
        if ss == 512 and disk.total_sectors > 2880:
            spt_g, heads_g = 17, 8
            cyls_g = disk.total_sectors // (spt_g * heads_g)
            if cyls_g > 0:
                return (cyls_g, heads_g, spt_g, ss)

        return None

    def _target_ext(self):
        val = self.fmt_combo.GetValue()
        if val.startswith("HDM"):
            return ".hdm"
        return f".{val.lower()}"

    # ── Events ───────────────────────────────────────────────────

    def _on_browse_source(self, event):
        dlg = wx.FileDialog(
            self, "Select Source Image",
            wildcard=IMAGE_WILDCARD,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        dlg.CentreOnParent()
        if dlg.ShowModal() == wx.ID_OK:
            self._load_source(dlg.GetPath())
        dlg.Destroy()

    def _on_browse_dest(self, event):
        ext = self._target_ext()
        name = "converted" + ext
        if self._source_path:
            stem = Path(self._source_path).stem
            name = stem + ext
        dlg = wx.FileDialog(
            self, "Save Converted Image As",
            defaultFile=name,
            wildcard=f"Image (*{ext})|*{ext}|All Files (*.*)|*.*",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT)
        dlg.CentreOnParent()
        if dlg.ShowModal() == wx.ID_OK:
            self._save_path = dlg.GetPath()
            self.save_ctrl.SetValue(self._save_path)
        dlg.Destroy()

    def _on_ok(self, event):
        if not self._source_disk:
            wx.MessageBox("Please select a source image.",
                          "No Source", wx.OK | wx.ICON_WARNING)
            return
        if not self._save_path:
            wx.MessageBox("Please choose where to save.",
                          "No Destination", wx.OK | wx.ICON_WARNING)
            return

        geom = self._detect_geometry()
        if geom is None:
            wx.MessageBox(
                "Cannot determine disk geometry.\n\n"
                "The source image has no geometry information and\n"
                "doesn\u2019t match any known floppy size.",
                "Unknown Geometry", wx.OK | wx.ICON_ERROR)
            return

        try:
            self._do_convert(geom)
            self.EndModal(wx.ID_OK)
        except Exception as exc:
            wx.MessageBox(
                f"Conversion failed:\n\n{exc}",
                "Error", wx.OK | wx.ICON_ERROR)

    def _do_convert(self, geom):
        """Read all sectors from source and write to the target format."""
        disk = self._source_disk
        cyls, heads, spt, ss = geom
        total = cyls * heads * spt

        # Read the flat raw data from the source image.
        raw = bytearray()
        for lba in range(total):
            raw.extend(disk.read_sector(lba))

        # Pad or truncate to exact geometry size.
        expected = total * ss
        if len(raw) < expected:
            raw.extend(b'\x00' * (expected - len(raw)))
        elif len(raw) > expected:
            raw = raw[:expected]

        fmt = self.fmt_combo.GetValue()
        path = self._save_path

        if fmt == "FDI":
            _write_fdi(path, raw, cyls, heads, spt, ss)
        elif fmt == "HDI":
            _write_hdi(path, raw, cyls, heads, spt, ss)
        elif fmt == "D88":
            _write_d88(path, raw, cyls, heads, spt, ss)
        else:
            # HDM / RAW — flat dump, no header.
            with open(path, 'wb') as f:
                f.write(raw)

    # ── Accessors ────────────────────────────────────────────────

    def get_save_path(self):
        return self._save_path


class BusyDialog(wx.Dialog):
    """A lightweight 'please wait' overlay that minimizes with its parent."""

    def __init__(self, parent, message):
        super().__init__(parent, title="",
                         style=wx.BORDER_SIMPLE | wx.FRAME_FLOAT_ON_PARENT)
        sizer = wx.BoxSizer(wx.VERTICAL)
        label = wx.StaticText(self, label=message)
        label.SetFont(label.GetFont().MakeLarger())
        sizer.Add(label, 0, wx.ALL, 20)
        self.SetSizerAndFit(sizer)
        self.CentreOnParent()
        self.Show()



# =============================================================================
# Main Frame
# =============================================================================

class PC98MountFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="PC-98 Disk Image Mounter",
                         size=(1060, 660))
        self.SetMinSize((800, 500))
        self._set_icon()

        self.images = []
        self._tree_paths = {}
        self._busy = False
        self._busy_dlg = None

        self._build_ui()
        self.Centre()

        # MountManager constructor runs stale-mount cleanup which
        # can be slow (VHD detach, subst removal, temp dir sweep).
        self._busy_dlg = BusyDialog(self, "Cleaning up stale mounts\u2026")
        wx.GetApp().Yield()
        self.mount_mgr = MountManager()
        self._busy_dlg.Destroy()
        self._busy_dlg = None

        self._update_mount_targets()

    # ── Icon ────────────────────────────────────────────────────────

    def _set_icon(self):
        """Load the application icon from cd-drive.png next to this script."""
        try:
            icon_path = Path(__file__).parent / "cd-drive.png"
            if icon_path.is_file():
                img = wx.Image(str(icon_path), wx.BITMAP_TYPE_PNG)
                # Create a multi-resolution icon bundle for crisp
                # display on both standard and high-DPI screens.
                bundle = wx.IconBundle()
                for size in (16, 32, 48, 64, 128):
                    scaled = img.Scale(size, size, wx.IMAGE_QUALITY_HIGH)
                    bmp = wx.Bitmap(scaled)
                    icon = wx.Icon()
                    icon.CopyFromBitmap(bmp)
                    bundle.AddIcon(icon)
                self.SetIcons(bundle)
        except Exception as exc:
            log.debug(f"Could not load icon: {exc}")

    # ── UI Construction ──────────────────────────────────────────────

    def _build_ui(self):
        self._build_menu_bar()

        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # --- Mount controls bar (top) ---
        mount_bar = wx.BoxSizer(wx.HORIZONTAL)

        lbl_target = "Drive:" if is_windows() else "Slot:"
        mount_bar.Add(wx.StaticText(panel, label=lbl_target),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.target_combo = wx.ComboBox(
            panel, size=(100 if is_windows() else 110, -1),
            style=wx.CB_READONLY)
        mount_bar.Add(self.target_combo, 0, wx.RIGHT, 4)

        if is_windows():
            btn_refresh = wx.Button(panel, label="\u21BB", size=(32, -1))
            btn_refresh.SetToolTip("Refresh available drive letters")
            btn_refresh.Bind(wx.EVT_BUTTON, self._on_refresh_targets)
            mount_bar.Add(btn_refresh, 0, wx.RIGHT, 8)

        mount_bar.Add(wx.StaticText(panel, label="Mode:"),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self.mode_combo = wx.ComboBox(
            panel, choices=["fat", "flat", "sectors"],
            value="fat", size=(90, -1), style=wx.CB_READONLY)
        mount_bar.Add(self.mode_combo, 0, wx.RIGHT, 8)

        btn_mount = wx.Button(panel, label="Mount")
        btn_mount.Bind(wx.EVT_BUTTON, self._on_mount)
        mount_bar.Add(btn_mount, 0, wx.RIGHT, 4)

        btn_unmount = wx.Button(panel, label="Unmount")
        btn_unmount.Bind(wx.EVT_BUTTON, self._on_unmount)
        mount_bar.Add(btn_unmount, 0, wx.RIGHT, 8)

        btn_convert = wx.Button(panel, label="Convert")
        btn_convert.SetToolTip(
            "Convert a disk image between formats\n"
            "(e.g. raw .img \u2192 .fdi/.hdi for Anex86)")
        btn_convert.Bind(wx.EVT_BUTTON, self._on_convert)
        mount_bar.Add(btn_convert, 0)

        main_sizer.Add(mount_bar, 0,
                        wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6)

        # --- Splitter: left sidebar | notebook ---
        splitter = wx.SplitterWindow(
            panel, style=wx.SP_LIVE_UPDATE)

        # Left: image list + button grid below
        left_panel = wx.Panel(splitter)
        left_sizer = wx.BoxSizer(wx.VERTICAL)

        left_sizer.Add(wx.StaticText(left_panel, label="Disk Images"),
                        0, wx.BOTTOM, 4)

        #  Image listbox (fills available space)
        self.image_listbox = wx.ListBox(left_panel)
        self.image_listbox.Bind(wx.EVT_LISTBOX, self._on_image_select)
        left_sizer.Add(self.image_listbox, 1, wx.EXPAND)

        #  Button grid (3 columns, pinned to bottom)
        btn_grid = wx.FlexGridSizer(cols=3, hgap=3, vgap=3)
        btn_grid.AddGrowableCol(0)
        btn_grid.AddGrowableCol(1)
        btn_grid.AddGrowableCol(2)
        for label, tip, handler in (
            ("Add",          "Open one or more disk images",
             self._on_add_image),
            ("Remove",       "Remove the selected image",
             self._on_remove_image),
            ("Blank Image",  "Create a new blank disk image",
             self._on_blank_image),
            ("Update Image", "Write changes back into the image",
             self._on_update),
            ("Explorer",     "Open the mounted directory",
             self._on_open_file_manager),
        ):
            btn = wx.Button(left_panel, label=label)
            btn.SetToolTip(tip)
            btn.Bind(wx.EVT_BUTTON, handler)
            btn_grid.Add(btn, 0, wx.EXPAND)

        left_sizer.Add(btn_grid, 0, wx.EXPAND | wx.TOP, 6)
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

        splitter.SetMinimumPaneSize(240)
        splitter.SplitVertically(left_panel, right_panel, 280)
        main_sizer.Add(splitter, 1,
                        wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6)

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

        self.info_label = wx.StaticText(
            page, label="No image loaded",
            style=wx.ST_ELLIPSIZE_END)
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

    # ── Menu Bar ────────────────────────────────────────────────────

    def _build_menu_bar(self):
        menu_bar = wx.MenuBar()

        # ── File menu ────────────────────────────────────────────
        file_menu = wx.Menu()
        item_add = file_menu.Append(wx.ID_ANY, "Add Image\u2026\tCtrl+O")
        item_remove = file_menu.Append(wx.ID_ANY, "Remove Image")
        file_menu.AppendSeparator()
        item_blank = file_menu.Append(
            wx.ID_ANY, "Blank Image\u2026\tCtrl+N")
        file_menu.AppendSeparator()
        item_quit = file_menu.Append(wx.ID_EXIT, "Quit\tCtrl+Q")

        self.Bind(wx.EVT_MENU, self._on_add_image, item_add)
        self.Bind(wx.EVT_MENU, self._on_remove_image, item_remove)
        self.Bind(wx.EVT_MENU, self._on_blank_image, item_blank)
        self.Bind(wx.EVT_MENU, self._on_menu_quit, item_quit)

        menu_bar.Append(file_menu, "&File")

        # ── Plugins menu ─────────────────────────────────────────
        plugins_menu = wx.Menu()
        item_mgr = plugins_menu.Append(
            wx.ID_ANY, "Plugin Manager\u2026\tCtrl+P")
        plugins_menu.AppendSeparator()
        item_install = plugins_menu.Append(
            wx.ID_ANY, "Install Plugin\u2026")
        item_open_dir = plugins_menu.Append(
            wx.ID_ANY, "Open Plugin Folder")
        plugins_menu.AppendSeparator()
        item_reload = plugins_menu.Append(
            wx.ID_ANY, "Reload All Plugins")

        self.Bind(wx.EVT_MENU, self._on_plugin_manager, item_mgr)
        self.Bind(wx.EVT_MENU, self._on_install_plugin, item_install)
        self.Bind(wx.EVT_MENU, self._on_open_plugin_folder, item_open_dir)
        self.Bind(wx.EVT_MENU, self._on_reload_all_plugins, item_reload)

        menu_bar.Append(plugins_menu, "&Plugins")

        # ── Help menu ────────────────────────────────────────────
        help_menu = wx.Menu()
        item_about = help_menu.Append(wx.ID_ABOUT, "About\u2026")
        self.Bind(wx.EVT_MENU, self._on_about, item_about)
        menu_bar.Append(help_menu, "&Help")

        self.SetMenuBar(menu_bar)

    # ── Menu event handlers ─────────────────────────────────────────

    def _on_menu_quit(self, event):
        self.Close()

    def _on_about(self, event):
        n_plugins = len(plugin_loader.get_loaded_plugins())
        n_fmts = len(registry.get_image_formats())
        n_det = len(registry.get_partition_detectors())
        n_fs = len(registry.get_filesystem_probers())
        wx.MessageBox(
            "PC-98 Disk Image Mounter\n\n"
            f"Loaded plugins: {n_plugins}\n"
            f"Image formats:  {n_fmts}\n"
            f"Partition detectors: {n_det}\n"
            f"Filesystem probers: {n_fs}\n\n"
            "Licensed under the MIT License.",
            "About", wx.OK | wx.ICON_INFORMATION,
        )

    def _on_plugin_manager(self, event):
        dlg = PluginManagerDialog(self)
        dlg.CentreOnParent()
        dlg.ShowModal()
        changed = dlg.plugins_changed
        dlg.Destroy()
        if changed:
            self._refresh_after_plugin_change()

    def _on_install_plugin(self, event):
        dlg = wx.FileDialog(
            self, "Select Plugin File",
            wildcard="Python files (*.py)|*.py",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        dlg.CentreOnParent()
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            info = plugin_loader.install_plugin_file(path)
            if info:
                self._set_status(
                    f"Installed plugin: {info.display_name}")
                self._refresh_after_plugin_change()
            else:
                wx.MessageBox(
                    f"Failed to install plugin:\n{path}",
                    "Plugin Error", wx.OK | wx.ICON_ERROR)
        dlg.Destroy()

    def _on_open_plugin_folder(self, event):
        plugin_dir = plugin_loader.get_plugin_dir()
        open_in_file_manager(str(plugin_dir))

    def _on_reload_all_plugins(self, event):
        loaded = plugin_loader.reload_all_plugins()
        self._refresh_after_plugin_change()
        self._set_status(
            f"Reloaded plugins: {len(loaded)} loaded")

    def _refresh_after_plugin_change(self):
        """Rebuild the file-dialog wildcard after plugins change."""
        global IMAGE_WILDCARD
        IMAGE_WILDCARD = _build_wildcard()
        log.info("File-dialog wildcard rebuilt after plugin change")

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

    # ── Blank image creation ─────────────────────────────────────────

    def _on_blank_image(self, event):
        """Show a dialog to create a new blank disk image."""
        dlg = BlankImageDialog(self)
        dlg.CentreOnParent()
        if dlg.ShowModal() == wx.ID_OK:
            fmt = dlg.get_format()
            geometry = dlg.get_geometry()
            format_fat = dlg.get_format_fat()
            save_path = dlg.get_path()

            if not save_path:
                dlg.Destroy()
                return

            try:
                create_blank_image(save_path, fmt, geometry, format_fat)
                self._load_image(save_path)

                # Build a descriptive status message
                if isinstance(geometry, tuple):
                    c, h, s, ss = geometry
                    total = c * h * s * ss
                    geom_str = (f"{c}C/{h}H/{s}S {ss}B "
                                f"({total / (1024*1024):.1f} MB)"
                                if total >= 1024*1024
                                else f"{c}C/{h}H/{s}S {ss}B "
                                     f"({total // 1024} KB)")
                else:
                    geom_str = geometry
                self._set_status(
                    f"Created blank {fmt} image: "
                    f"{Path(save_path).name} ({geom_str})")
            except Exception as e:
                wx.MessageBox(
                    f"Failed to create blank image:\n\n{e}",
                    "Error", wx.OK | wx.ICON_ERROR)
                self._set_status(f"Blank image creation failed: {e}")
        dlg.Destroy()

    def _load_image(self, path):
        self._set_status(f"Loading {Path(path).name}\u2026")
        try:
            disk = open_image(path)
            fs = None
            try:
                fs = registry.probe_filesystem(disk)
            except Exception as e:
                log.info(f"No filesystem detected: {e}")

            label = ((fs.volume_label if fs and fs.volume_label else None)
                     or disk.label or Path(path).stem)

            info = ImageInfo(path, disk, fs, label)
            self.images.append(info)

            self.image_listbox.Append(Path(path).name)
            self.image_listbox.SetSelection(len(self.images) - 1)
            self._on_image_select(None)

            total_kb = disk.total_sectors * disk.sector_size // 1024
            fs_str = fs.fs_type_label if fs else "No FAT"
            file_count = (
                sum(1 for _, e in fs.walk() if not e.is_directory)
                if fs else 0
            )
            status = (
                f"Loaded {Path(path).name}: {fs_str}, "
                f"{disk.total_sectors} sectors \u00d7 "
                f"{disk.sector_size}B = {total_kb} KB"
            )
            if fs:
                if not fs.can_write_back:
                    status += (
                        f" \u2014 \u26a0 {fs.write_back_status}"
                        f" (read-only, write-back disabled)")
                elif file_count > 0:
                    status += f", {file_count} files found"
                else:
                    status += (f" \u2014 WARNING: {fs_str} parsed but "
                               "0 files found!")
            else:
                status += " (raw access only)"
            self._set_status(status)

            # Show a dialog warning for unreliable filesystem detection
            # so users don't accidentally corrupt non-FAT images
            # (e.g. PC-8801 D88 with N88-BASIC filesystem).
            if fs and not fs.can_write_back:
                wx.MessageBox(
                    f"No valid filesystem was reliably detected.\n\n"
                    f"{fs.write_back_status}.\n\n"
                    f"This may be a non-FAT disk\n"
                    f"(e.g. PC-8801 N88-BASIC format).\n\n"
                    f"You can browse files (if any were found), but\n"
                    f"write-back is disabled to prevent corruption.",
                    "Filesystem Not Detected",
                    wx.OK | wx.ICON_WARNING)
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
            f"{info.label} \u2014 {info.fs.fs_type_label}, "
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
            f"Disk Label:      {info.disk.label}",
            f"Sector Size:     {info.disk.sector_size} bytes",
            f"Total Sectors:   {info.disk.total_sectors}",
            f"Total Size:      "
            f"{info.disk.total_sectors * info.disk.sector_size:,} bytes",
            "",
        ]

        if info.fs:
            fs = info.fs
            # Show volume label with raw hex for debugging
            vol = fs.volume_label or "(none)"
            try:
                raw = info.fs._read_fs_bytes(0x2B, 11)
                vol_hex = ' '.join(f'{b:02X}' for b in raw)
                vol_line = f"{vol}  [{vol_hex}]"
            except Exception:
                vol_line = vol
            lines += [
                "\u2500\u2500 FAT Filesystem \u2500" * 3,
                f"Volume Label:    {vol_line}",
                f"Filesystem:      {fs.fs_type_label}",
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

    # ── Async helpers ───────────────────────────────────────────────

    def _show_busy(self, message):
        """Show a 'please wait' dialog that follows the main window."""
        self._busy = True
        self._busy_dlg = BusyDialog(self, message)

    def _dismiss_busy(self):
        """Dismiss the busy dialog."""
        if self._busy_dlg:
            self._busy_dlg.Destroy()
            self._busy_dlg = None
        self._busy = False

    # ── Mount / Unmount ──────────────────────────────────────────────

    def _on_mount(self, event):
        if self._busy:
            return
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

        if (mode == "fat" and info.fs
                and not info.fs.can_write_back):
            answer = wx.MessageBox(
                "No valid filesystem was reliably detected.\n\n"
                f"{info.fs.write_back_status}.\n\n"
                f"You can mount it read-only, but write-back will be\n"
                f"disabled to prevent corruption.\n\n"
                f"Mount anyway?",
                "Filesystem Not Detected",
                wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
            if answer != wx.YES:
                return

        self._set_status(
            f"Mounting {Path(info.path).name} at {target} ({mode})\u2026")
        self._show_busy("Please wait, mounting in progress\u2026")

        image_size = info.disk.total_sectors * info.disk.sector_size

        def _work():
            try:
                self.mount_mgr.mount(
                    target, mode,
                    disk_image=info.disk,
                    fat_fs=info.fs,
                    image_size_bytes=image_size,
                )
                wx.CallAfter(self._mount_done, info, target, mode, None)
            except Exception as e:
                wx.CallAfter(self._mount_done, info, target, mode, e)

        threading.Thread(target=_work, daemon=True).start()

    def _mount_done(self, info, target, mode, error):
        self._dismiss_busy()

        if error:
            wx.MessageBox(f"Failed to mount:\n\n{error}",
                          "Mount Error", wx.OK | wx.ICON_ERROR)
            self._set_status(f"Mount failed: {error}")
            return

        info.mount_id = target
        info.mount_mode = mode

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

    def _on_unmount(self, event):
        if self._busy:
            return
        info = self._selected_image()
        if not info:
            return
        if (not info.mount_id
                or not self.mount_mgr.is_mounted(info.mount_id)):
            wx.MessageBox("This image is not mounted.",
                          "Not Mounted", wx.OK | wx.ICON_INFORMATION)
            return

        mid = info.mount_id
        self._set_status(f"Unmounting {mid}\u2026")
        self._show_busy("Please wait, unmounting\u2026")

        def _work():
            try:
                self.mount_mgr.unmount(mid)
                wx.CallAfter(self._unmount_done, info, mid, None)
            except Exception as e:
                wx.CallAfter(self._unmount_done, info, mid, e)

        threading.Thread(target=_work, daemon=True).start()

    def _unmount_done(self, info, mid, error):
        self._dismiss_busy()

        if error:
            wx.MessageBox(f"Failed to unmount:\n\n{error}",
                          "Unmount Error", wx.OK | wx.ICON_ERROR)
            self._set_status(f"Unmount failed: {error}")
            return

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

    # ── Convert ─────────────────────────────────────────────────────

    def _on_convert(self, event):
        """Open the Convert dialog, optionally pre-loading the currently
        selected image as the source."""
        info = self._selected_image()
        source_path = info.path if info else None
        source_disk = info.disk if info else None

        dlg = ConvertDialog(self,
                            source_path=source_path,
                            source_disk=source_disk)
        dlg.CentreOnParent()
        result = dlg.ShowModal()
        save_path = dlg.get_save_path()
        dlg.Destroy()

        if result == wx.ID_OK and save_path:
            self._set_status(f"Converted \u2192 {save_path}")
            # Offer to open the converted image.
            answer = wx.MessageBox(
                f"Image converted successfully:\n{save_path}\n\n"
                f"Open the converted image now?",
                "Conversion Complete",
                wx.YES_NO | wx.ICON_INFORMATION)
            if answer == wx.YES:
                self._load_image(save_path)

    # ── Update (write-back) ──────────────────────────────────────────

    def _on_update(self, event):
        """Write modifications from the mount point back into the
        disk image, offering "Overwrite" / "Save As" / "Cancel"."""
        if self._busy:
            return
        info = self._selected_image()
        if not info:
            wx.MessageBox("Select a disk image first.",
                          "No Image", wx.OK | wx.ICON_INFORMATION)
            return

        if (not info.mount_id
                or not self.mount_mgr.is_mounted(info.mount_id)):
            wx.MessageBox(
                "This image is not currently mounted.\n"
                "Mount it first, make your changes in the file "
                "manager, then click Update.",
                "Not Mounted", wx.OK | wx.ICON_INFORMATION)
            return

        # Block write-back if filesystem parameters are unreliable.
        if (info.mount_mode == 'fat' and info.fs
                and not info.fs.can_write_back):
            wx.MessageBox(
                "Write-back is disabled for this image.\n\n"
                f"{info.fs.write_back_status}.\n\n"
                f"Writing back with unreliable parameters would\n"
                f"corrupt the image.\n\n"
                f"You can still use 'flat' or 'sectors' mode for\n"
                f"raw access.",
                "Write-Back Disabled",
                wx.OK | wx.ICON_ERROR)
            return

        # --- Confirmation dialog with three choices ---
        dlg = wx.MessageDialog(
            self,
            f"Write changes back to the disk image?\n\n"
            f"Image:  {Path(info.path).name}\n"
            f"Mode:   {info.mount_mode}\n\n"
            f"\"Yes\" overwrites the original file.\n"
            f"\"No\" lets you choose a new file (Save As).\n"
            f"\"Cancel\" does nothing.",
            "Update Disk Image",
            wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION
        )
        dlg.SetYesNoLabels("Overwrite Original", "Save As\u2026")
        dlg.CentreOnParent()
        choice = dlg.ShowModal()
        dlg.Destroy()

        if choice == wx.ID_CANCEL:
            return

        save_path = None  # None means overwrite original

        if choice == wx.ID_NO:
            # "Save As" was clicked.
            wildcard = "All Files (*.*)|*.*"
            default_name = Path(info.path).name
            save_dlg = wx.FileDialog(
                self, "Save Image As",
                defaultDir=str(Path(info.path).parent),
                defaultFile=default_name,
                style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
                wildcard=wildcard,
            )
            save_dlg.CentreOnParent()
            if save_dlg.ShowModal() != wx.ID_OK:
                save_dlg.Destroy()
                return
            save_path = save_dlg.GetPath()
            save_dlg.Destroy()

        # --- Perform the write-back ---
        self._set_status("Writing changes back to image\u2026")
        self._show_busy(
            "Please wait, writing changes to disk image\u2026")

        def _work():
            try:
                result = self.mount_mgr.update(
                    info.mount_id,
                    info.mount_mode,
                    disk_image=info.disk,
                    fat_fs=info.fs,
                    save_path=save_path,
                )
                wx.CallAfter(self._update_done,
                             info, save_path, result, None)
            except Exception as e:
                wx.CallAfter(self._update_done,
                             info, save_path, None, e)

        threading.Thread(target=_work, daemon=True).start()

    def _update_done(self, info, save_path, result, error):
        self._dismiss_busy()

        if error:
            log.exception("Update failed")
            wx.MessageBox(
                f"Failed to write changes back:\n\n{error}",
                "Update Error", wx.OK | wx.ICON_ERROR)
            self._set_status(f"Update failed: {error}")
            return

        self._set_status(result)
        self._populate_tree(info)
        self._populate_detail(info)

        dest = save_path or info.path
        wx.MessageBox(
            f"Update complete.\n\n{result}\n\n"
            f"Saved to:\n{dest}",
            "Update Successful",
            wx.OK | wx.ICON_INFORMATION,
        )

    # ── Open in file manager ─────────────────────────────────────────

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
        if self._busy:
            return
        if not self.mount_mgr._mounts:
            self.Destroy()
            return

        self._busy = True
        self._busy_dlg = BusyDialog(self, "Unmounting, please wait\u2026")

        def _work():
            try:
                self.mount_mgr.unmount_all()
            except Exception:
                pass
            wx.CallAfter(self.Destroy)

        threading.Thread(target=_work, daemon=True).start()


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