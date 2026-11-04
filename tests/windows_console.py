"""Send a real Ctrl+C to a test server's separate Windows console."""

import ctypes
import sys
from ctypes import wintypes


def interrupt_console(pid):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = (
        ("FreeConsole", [], wintypes.BOOL),
        ("AttachConsole", [wintypes.DWORD], wintypes.BOOL),
        ("SetConsoleCtrlHandler", [ctypes.c_void_p, wintypes.BOOL], wintypes.BOOL),
        ("GenerateConsoleCtrlEvent", [wintypes.DWORD, wintypes.DWORD], wintypes.BOOL),
        ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    )
    for name, arguments, result in signatures:
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        kernel.FreeConsole()
        if not kernel.AttachConsole(pid):
            raise ctypes.WinError(ctypes.get_last_error())
        # Only this helper ignores Ctrl+C. The server must handle the real event.
        if not kernel.SetConsoleCtrlHandler(None, True):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.GenerateConsoleCtrlEvent(0, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel.WaitForSingleObject(handle, 15000) != 0:
            raise RuntimeError("The test server did not exit after Ctrl+C")
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    interrupt_console(int(sys.argv[1]))
