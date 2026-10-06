# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files

assets = [('models/g2pw', 'models/g2pw'), ('assets', 'assets')]
for package in ('rapidocr_onnxruntime', 'imageio_ffmpeg'):
    assets += sorted(collect_data_files(package))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=assets,
    hiddenimports=['edge_tts'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SmartVoice',
    icon='assets/smartvoice.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='SmartVoice',
)
