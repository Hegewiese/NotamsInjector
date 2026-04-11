# PyInstaller spec file for NOTAM Injector
# Run: pyinstaller build.spec

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# SimConnect.dll ships inside the Python SimConnect package — no SDK needed.
try:
    import SimConnect as _sc_pkg
    _simconnect_dll = os.path.join(os.path.dirname(_sc_pkg.__file__), "SimConnect.dll")
    _extra_binaries = [(_simconnect_dll, ".")] if os.path.isfile(_simconnect_dll) else []
except ImportError:
    _extra_binaries = []

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=_extra_binaries,
    datas=[
        # Bundle config, airports data, and assets
        ('config.yaml',    '.'),
        ('data/airports.csv', 'data'),
        ('assets',         'assets'),
    ],
    hiddenimports=[
        # PySide6 plugins often need explicit listing
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        # SimConnect DLL wrapper
        'SimConnect',
        # aiosqlite / asyncio internals
        'aiosqlite',
        'asyncio',
        # pydantic-settings yaml support
        'pydantic_settings',
        'yaml',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim unnecessary packages from the bundle
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'PIL',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='NotamInjector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # no console window — we are a tray app
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/icon.ico',
    # version='version_info.txt'  # uncomment after creating version_info.txt
)
