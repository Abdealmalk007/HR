# -*- mode: python ; coding: utf-8 -*-
# يُشغَّل هذا الملف فقط عبر: pyinstaller build_assets/ProTech_HR.spec
# على جهاز Windows حقيقي فيه Python 3.11+ وحزمة PyInstaller مثبّتة.
# لا يمكن تشغيله على Linux/macOS لإنتاج exe لويندوز (لا يوجد تصريف متقاطع).

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None
ROOT = Path(SPECPATH).resolve().parent  # SPECPATH هو مجلد build_assets، ووالده جذر المشروع
VENDOR = ROOT / "vendor"  # مكتبات XLS والتاريخ الهجري المضمّنة للعمل دون إنترنت
if sys.maxsize <= 2**32:
    raise SystemExit("يتطلب بناء ProTech HR v23 مفسر Python ‏64-bit.")
TK_RUNTIME = ROOT / "build_assets" / "tk_runtime"


def data_tree(source, destination):
    source = Path(source)
    return [
        (str(path), str(Path(destination) / path.parent.relative_to(source)))
        for path in source.rglob("*")
        if path.is_file()
    ]
a = Analysis(
    [str(ROOT / "run_hr.pyw")],
    pathex=[str(ROOT), str(VENDOR)],
    binaries=[
        (str(TK_RUNTIME / "_tkinter.pyd"), "."),
        (str(TK_RUNTIME / "tcl86t.dll"), "."),
        (str(TK_RUNTIME / "tk86t.dll"), "."),
        (str(TK_RUNTIME / "zlib1.dll"), "."),
    ],
    datas=[
        (str(ROOT / "assets" / "protech_hr.ico"), "assets"),
        (str(ROOT / "assets" / "protech_logo.png"), "assets"),
        (str(ROOT / "assets" / "protech_mark.png"), "assets"),
        (str(ROOT / "assets" / "fonts" / "DejaVuSans.ttf"), "assets/fonts"),
        (str(ROOT / "assets" / "fonts" / "LICENSE_DEJAVU.txt"), "assets/fonts"),
        *data_tree(TK_RUNTIME / "_tcl_data", "_tcl_data"),
        *data_tree(TK_RUNTIME / "_tk_data", "_tk_data"),
        *collect_data_files("reportlab"),
    ],
    hiddenimports=[
        "xlrd",
        "xlrd.book",
        "xlrd.sheet",
        "hijridate",
        "reportlab",
        "reportlab.platypus",
        "reportlab.pdfbase.ttfonts",
        "arabic_reshaper",
        "bidi.algorithm",
        "_tkinter",
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.simpledialog",
    ],
    hookspath=[str(ROOT / "build_assets" / "hooks")],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "build_assets" / "pyi_rth_protech_tk.py")],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ProTech_HR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ROOT / "assets" / "protech_hr.ico"),
    version=str(ROOT / "build_assets" / "version_info.txt"),
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ProTech_HR_Portable",
)
