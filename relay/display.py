"""Enable native rendering at the Windows system DPI before creating any Tk window."""
import ctypes
import sys


def enable_native_dpi():
    """Return whether the process is DPI-aware; preserve any existing awareness setting."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if user32.IsProcessDPIAware():
            return True
    except (AttributeError, OSError):
        return False
    try:
        set_context = user32.SetProcessDpiAwarenessContext
    except AttributeError:
        try:
            set_awareness = ctypes.WinDLL("shcore").SetProcessDpiAwareness
        except (AttributeError, OSError):
            try:
                return bool(user32.SetProcessDPIAware())
            except AttributeError:
                return False
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        return set_awareness(1) == 0  # PROCESS_SYSTEM_DPI_AWARE; failure preserves the manifest.
    set_context.argtypes = [ctypes.c_void_p]
    set_context.restype = ctypes.c_int
    # A failed call can mean an explicit manifest already fixed the mode; do not override it.
    return bool(set_context(ctypes.c_void_p(-2)))  # DPI_AWARENESS_CONTEXT_SYSTEM_AWARE
