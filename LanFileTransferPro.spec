# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

project_dir = Path(__file__).resolve().parent
icon_path = project_dir / 'assets' / 'app.ico'

a = Analysis(
    ['main.py'],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[
        (str(project_dir / 'assets'), 'assets'),
        (str(project_dir / 'uploads'), 'uploads'),
        (str(project_dir / 'shared_files'), 'shared_files'),
        (str(project_dir / 'config.json'), '.'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LanFileTransferPro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=str(icon_path),
)
