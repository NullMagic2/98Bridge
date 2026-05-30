# 98Bridge — PC-98 Disk Image Mounter

Mount, browse, and extract files from NEC PC-98 floppy and hard disk images
on modern Windows and Linux systems.

<img width="2094" height="1290" alt="image" src="https://github.com/user-attachments/assets/06d849d7-ff60-488f-b7b5-682c02b03d49" />


![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.10%2B-brightgreen.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20WSL-lightgrey.svg)

## Features

- **Browse FAT12/FAT16 filesystems** with a built-in file tree —
  view files, sizes, dates, and attributes from PC-98 disk images.
- **Mount as a drive letter** (Windows) or directory (Linux/WSL) —
  drag-and-drop files with Explorer or any file manager.
- **Three mount modes**: FAT (file-level), Flat (raw image as one file),
  Sectors (each sector as an individual file).
- **Write back** changes from the mounted directory into the disk image.
- **Built-in hex viewer** for sector-level inspection with bookmarks,
  search, and PC-98 structure annotations.
- **Create blank images** with configurable geometry (floppy or HDD)
  and optional FAT formatting.
- **Plugin system** for adding new image formats, partition schemes,
  and filesystems without modifying core code.

## Supported formats

| Format | Extensions | Type | Source |
|--------|-----------|------|--------|
| D88/D68/D77 | `.d88` `.d68` `.d77` | Floppy | Core plugin |
| FDI | `.fdi` | Floppy | Core plugin |
| HDI (Anex86 / T98-Next) | `.hdi` | Hard disk | Core plugin |
| HDM / TFD | `.hdm` `.tfd` | Floppy (raw 1024B sectors) | Core plugin |
| Raw / IMG | `.img` `.ima` | Any | Core plugin |
| NHD (T98-Next) | `.nhd` | Hard disk | Bundled plugin |

Additional formats can be added by dropping a plugin file into `plugins/`.

## Requirements

- Python 3.10 or later
- wxPython (`pip install wxPython`)

On Windows, mounting uses VHD (requires admin) or `subst` (no admin).
On Linux/WSL, files are extracted to a browsable directory.

## Quick start

```bash
# Install wxPython
pip install wxPython

# Launch the GUI
python pc98mount.py

# Or open an image directly
python pc98mount.py my_disk.d88

# Mount to a drive letter (Windows)
python pc98mount.py my_disk.d88 -d P --mode fat

# Mount to a named slot (Linux/WSL)
python pc98mount.py my_disk.d88 -n game --mode fat
```

## Project structure

```
98Bridge/
├── pc98mount.py          Main GUI application (wxPython)
├── disk_image.py         Image format parsers (D88, FDI, HDI, Raw)
├── fat_fs.py             FAT12/FAT16 filesystem parser
├── partition.py          Partition table detection (MBR, PC-98 IPL)
├── registry.py           Plugin registry (formats, detectors, probers)
├── plugin_loader.py      Plugin discovery, loading, enable/disable
├── plugin_manager.py     Plugin Manager dialog (wxPython)
├── hex_viewer.py         Sector-level hex viewer widget
├── mount_backend.py      Mount strategies (VHD, subst, directory)
├── 98Bridge.config       Plugin state (auto-created, JSON)
├── LICENSE               MIT License
├── README.md             This file
├── make_nhd.py           Test image generator for NHD format
└── plugins/
    ├── core/
    │   ├── pc98_formats.py      Built-in image formats + partition detectors
    │   └── fat_filesystem.py    Built-in FAT12/FAT16 prober
    ├── nhd_format.py            NHD (T98-Next) support (removable)
    └── README.md                Plugin development guide
```

## The plugin system

98Bridge uses a fully pluggable architecture. Even the built-in PC-98
formats and FAT filesystem are loaded as plugins, making the system
completely modular.

### Core vs. user plugins

- **Core plugins** (`plugins/core/`) are essential for the program to
  function. They appear grayed out in the Plugin Manager and cannot be
  removed or disabled.
- **User plugins** (`plugins/`) can be freely installed, removed,
  enabled, or disabled at any time.

### Managing plugins

Open **Plugins → Plugin Manager** (Ctrl+P) to:

- **Install** a plugin from a `.py` file
- **Disable / Enable** a user plugin (persisted in `98Bridge.config`)
- **Remove** a user plugin (deletes the file from disk)
- **Reload** a single plugin or all plugins after editing

### Writing a plugin

A plugin is a `.py` file that imports from the 98Bridge modules and
calls registration functions:

```python
from disk_image import DiskImage
from registry import register_image_format

class MyImage(DiskImage):
    def _parse(self):
        self._sector_size = 512
        self._total_sectors = len(self._data) // 512
        self._label = "My Image"

    def read_sector(self, lba):
        off = lba * self._sector_size
        return bytes(self._data[off:off + self._sector_size])

    def write_sector(self, lba, data):
        off = lba * self._sector_size
        self._data[off:off + self._sector_size] = data[:self._sector_size]

register_image_format(
    extensions=['.xyz'],
    opener=MyImage,
    label='My Format',
    priority=10,
)
```

Three types of components can be registered:

| Function | What it registers |
|----------|-------------------|
| `register_image_format()` | A new disk image container format |
| `register_partition_detector()` | A new partition table scheme |
| `register_filesystem_prober()` | A new filesystem type |

See `plugins/nhd_format.py` for a complete real-world example.

## Mount modes

| Mode | Description | Use case |
|------|-------------|----------|
| `fat` | Extracts files and folders from the FAT filesystem | Browsing and editing game files |
| `flat` | Exposes the entire raw image as a single `DISK.IMG` file | Hex editing or backup |
| `sectors` | Each sector as an individual `SECTOR_NNNN.BIN` file | Low-level analysis |

## Configuration

Plugin state is stored in `98Bridge.config`, a JSON file next to the
program. It is created automatically when you first disable a plugin:

```json
{
  "disabled_plugins": ["nhd_format"]
}
```

Delete this file to reset all plugins to their default (enabled) state.

## License

MIT License. See [LICENSE](LICENSE) for details.
