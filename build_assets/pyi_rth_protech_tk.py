"""Runtime hook for environments where PyInstaller cannot auto-detect Tcl/Tk."""

import ctypes
import os
from pathlib import Path
import sys

bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
tcl_library = bundle_root / "_tcl_data"
tk_library = bundle_root / "_tk_data"
os.environ["TCL_LIBRARY"] = tcl_library.as_posix()
os.environ["TK_LIBRARY"] = tk_library.as_posix()


def _probe(message: str) -> None:
    if os.environ.get("PROTECH_HR_UI_SMOKE_TEST", "").strip() != "1":
        return
    target = Path(os.environ.get("PROTECH_HR_DATA_DIR", bundle_root)) / "runtime_hook_probe.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")

# بعض حزم Tcl على Windows لا تهيئ طبقة نظام الملفات قبل Tcl_Init عندما
# يعمل التطبيق من مجلد مجمّد. إجراء قراءة آمنة لوجود init.tcl داخل مفسر
# مؤقت يهيئ الطبقة نفسها، ثم تستخدمها tkinter بصورة طبيعية.
_probe("runtime hook started")
_probe(
    f"bundle_root={bundle_root.as_posix()} "
    f"init_tcl={(tcl_library / 'init.tcl').as_posix()} "
    f"python_exists={(tcl_library / 'init.tcl').is_file()}"
)
tcl_dll = ctypes.WinDLL(str(bundle_root / "tcl86t.dll"))
_probe("Tcl DLL loaded")
tcl_dll.Tcl_FindExecutable.argtypes = [ctypes.c_char_p]
tcl_dll.Tcl_FindExecutable.restype = None
tcl_dll.Tcl_CreateInterp.argtypes = []
tcl_dll.Tcl_CreateInterp.restype = ctypes.c_void_p
tcl_dll.Tcl_Eval.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
tcl_dll.Tcl_Eval.restype = ctypes.c_int
tcl_dll.Tcl_SetSystemEncoding.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
tcl_dll.Tcl_SetSystemEncoding.restype = ctypes.c_int
tcl_dll.Tcl_GetStringResult.argtypes = [ctypes.c_void_p]
tcl_dll.Tcl_GetStringResult.restype = ctypes.c_char_p
tcl_dll.Tcl_DeleteInterp.argtypes = [ctypes.c_void_p]
tcl_dll.Tcl_DeleteInterp.restype = None
tcl_dll.Tcl_FindExecutable(str(Path(sys.executable)).encode("utf-8"))
_probe("Tcl executable registered")
interp = tcl_dll.Tcl_CreateInterp()
_probe("temporary Tcl interpreter created")
try:
    encoding_code = tcl_dll.Tcl_SetSystemEncoding(interp, b"utf-8")
    _probe(f"Tcl system encoding initialized: {encoding_code}")
    init_tcl = (tcl_library / "init.tcl").as_posix()
    # استدعاء pwd أولًا ضروري في بعض توزيعات Tcl على Windows لتهيئة
    # نظام الملفات الأصلي قبل أول فحص لمسار مطلق.
    command = f"list [pwd] [file exists {{{init_tcl}}}]".encode("utf-8")
    result_code = tcl_dll.Tcl_Eval(interp, command)
    result_text = tcl_dll.Tcl_GetStringResult(interp) or b""
    _probe(f"Tcl file probe returned {result_code}: {result_text!r}")
    if result_code != 0 or not result_text.rstrip().endswith(b" 1"):
        raise RuntimeError(f"تعذر تهيئة مكتبة Tcl المضمنة: {result_text.decode('utf-8', 'replace')}")
finally:
    tcl_dll.Tcl_DeleteInterp(interp)
    _probe("temporary Tcl interpreter deleted")
