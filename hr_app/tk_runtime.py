from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys

_DLL_DIRECTORY_HANDLES: list[object] = []
_LOADED_TCL_DLLS: list[object] = []
_CONFIGURED = False


def _existing_runtime_from_environment() -> tuple[Path, Path, Path | None] | None:
    tcl_text = os.environ.get("TCL_LIBRARY", "").strip()
    tk_text = os.environ.get("TK_LIBRARY", "").strip()
    if not tcl_text or not tk_text:
        return None
    tcl_library = Path(tcl_text)
    tk_library = Path(tk_text)
    if (tcl_library / "init.tcl").is_file() and (tk_library / "tk.tcl").is_file():
        common_parent = tcl_library.parent if tcl_library.parent == tk_library.parent else None
        return tcl_library, tk_library, common_parent
    return None


def _python_runtime_candidates() -> list[tuple[Path, Path, Path | None]]:
    candidates: list[tuple[Path, Path, Path | None]] = []
    seen: set[Path] = set()
    for base in (Path(sys.base_prefix), Path(sys.prefix), Path(sys.exec_prefix)):
        try:
            base = base.resolve()
        except OSError:
            continue
        if base in seen:
            continue
        seen.add(base)
        candidates.append((base / "tcl" / "tcl8.6", base / "tcl" / "tk8.6", base / "DLLs"))
    return candidates


def _bundled_runtime_candidates() -> list[tuple[Path, Path, Path | None]]:
    project_root = Path(__file__).resolve().parents[1]
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    portable_root = Path(sys.executable).resolve().parent
    runtime_root = project_root / "build_assets" / "tk_runtime"
    return [
        (bundle_root / "_tcl_data", bundle_root / "_tk_data", bundle_root),
        (portable_root / "_tcl_data", portable_root / "_tk_data", portable_root),
        (runtime_root / "_tcl_data", runtime_root / "_tk_data", runtime_root),
    ]


def _available_runtime_candidates() -> list[tuple[Path, Path, Path | None]]:
    candidates: list[tuple[Path, Path, Path | None]] = []
    if current := _existing_runtime_from_environment():
        candidates.append(current)
    if getattr(sys, "frozen", False):
        candidates.extend(_bundled_runtime_candidates())
        candidates.extend(_python_runtime_candidates())
    else:
        candidates.extend(_python_runtime_candidates())
        candidates.extend(_bundled_runtime_candidates())
    unique: list[tuple[Path, Path, Path | None]] = []
    seen: set[tuple[Path, Path, Path | None]] = set()
    for tcl_library, tk_library, dll_dir in candidates:
        key = (tcl_library, tk_library, dll_dir)
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _warm_windows_tcl(dll_dir: Path | None, tcl_library: Path) -> None:
    if os.name != "nt":
        return
    dll_candidates = []
    if dll_dir is not None:
        dll_candidates.append(dll_dir / "tcl86t.dll")
        if hasattr(os, "add_dll_directory") and dll_dir.is_dir():
            try:
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(dll_dir)))
            except (OSError, AttributeError):
                pass
    for base in (Path(sys.base_prefix), Path(sys.prefix)):
        dll_candidates.append(base / "DLLs" / "tcl86t.dll")

    tcl_dll_path = next((path for path in dll_candidates if path.is_file()), None)
    if tcl_dll_path is None:
        return

    tcl_dll = ctypes.WinDLL(str(tcl_dll_path))
    _LOADED_TCL_DLLS.append(tcl_dll)
    tcl_dll.Tcl_FindExecutable.argtypes = [ctypes.c_char_p]
    tcl_dll.Tcl_FindExecutable.restype = None
    tcl_dll.Tcl_CreateInterp.argtypes = []
    tcl_dll.Tcl_CreateInterp.restype = ctypes.c_void_p
    tcl_dll.Tcl_Eval.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    tcl_dll.Tcl_Eval.restype = ctypes.c_int
    tcl_dll.Tcl_GetStringResult.argtypes = [ctypes.c_void_p]
    tcl_dll.Tcl_GetStringResult.restype = ctypes.c_char_p
    tcl_dll.Tcl_DeleteInterp.argtypes = [ctypes.c_void_p]
    tcl_dll.Tcl_DeleteInterp.restype = None

    tcl_dll.Tcl_FindExecutable(str(Path(sys.executable)).encode("utf-8"))
    interp = tcl_dll.Tcl_CreateInterp()
    if not interp:
        return
    try:
        init_tcl = (tcl_library / "init.tcl").as_posix()
        for command in ("pwd", "file exists {C:/Windows}", f"file exists {{{init_tcl}}}"):
            tcl_dll.Tcl_Eval(interp, command.encode("utf-8"))
    finally:
        tcl_dll.Tcl_DeleteInterp(interp)


def configure_tk_runtime() -> Path | None:
    """Prepare Tcl/Tk before importing or creating Tk windows.

    Some portable Python environments include Tcl/Tk files but do not register
    the Tcl executable early enough for Tk to find init.tcl.  This setup keeps
    normal Python installs untouched while making source and portable runs use
    the best available runtime automatically.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return Path(os.environ["TCL_LIBRARY"]) if os.environ.get("TCL_LIBRARY") else None

    for tcl_library, tk_library, dll_dir in _available_runtime_candidates():
        if not (tcl_library / "init.tcl").is_file() or not (tk_library / "tk.tcl").is_file():
            continue
        os.environ["TCL_LIBRARY"] = tcl_library.as_posix()
        os.environ["TK_LIBRARY"] = tk_library.as_posix()
        if dll_dir is not None and dll_dir.is_dir():
            current_path = os.environ.get("PATH", "")
            dll_text = str(dll_dir)
            if dll_text not in current_path.split(os.pathsep):
                os.environ["PATH"] = dll_text + os.pathsep + current_path
        try:
            _warm_windows_tcl(dll_dir, tcl_library)
        except Exception:
            pass
        _CONFIGURED = True
        return tcl_library
    _CONFIGURED = True
    return None
