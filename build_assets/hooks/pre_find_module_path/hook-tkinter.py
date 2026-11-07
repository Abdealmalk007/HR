"""Keep tkinter discoverable when the host Tcl library cannot initialize."""


def pre_find_module_path(hook_api):
    # The v18 spec packages Tcl/Tk data and binaries explicitly. Leaving
    # search_dirs unchanged allows PyInstaller to collect Python's tkinter
    # package even when its automatic TclTkInfo probe is unavailable.
    return None
