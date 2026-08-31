# -*- mode: python ; coding: utf-8 -*-

import os
import rapidocr
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# audio_separator runs inside this exe via `main.py --run-audio-separator`
# (see modules/separator._separator_executable). Pull in its dynamically
# imported architecture/model modules and its bundled data.
_audio_separator_hidden = collect_submodules('audio_separator')
_audio_separator_data = collect_data_files('audio_separator')

# Bundle the operator's config.local.json (gitignored) into _internal so a
# fresh build ships the working Gemini key/base_url. config.py picks it up via
# the _MEIPASS candidate; a config.local.json placed next to the exe still
# overrides it. Rotate the key by editing this file, bumping VERSION and
# publishing a new KPHOTO-YTB_update.zip.
_local_config = [('config.local.json', '.')] if os.path.isfile('config.local.json') else []


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('models', 'models'),
        *collect_data_files('rapidocr', include_py_files=False),
        *_audio_separator_data,
        *_local_config,
    ],
    hiddenimports=[
        *_audio_separator_hidden,
        'onnxruntime',
    ],
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
    name='KPHOTO-YTB',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/kphoto-ytb.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='KPHOTO-YTB',
)
