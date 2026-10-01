"""Identify and control one local Windows EPLAN process without PID-reuse races."""

import ctypes
import ntpath
import subprocess
from ctypes import wintypes


def _listener_pid(port):
    port = str(int(port))
    result = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                            capture_output=True, text=True, timeout=30, check=True)
    owners = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if (len(fields) == 5 and fields[0] == "TCP" and fields[3] == "LISTENING"
                and fields[1].rsplit(":", 1)[-1] == port and fields[4].isdigit()):
            owners.add(int(fields[4]))
    if len(owners) != 1:
        raise RuntimeError(f"Cannot identify a unique local process listening on port {port}.")
    return owners.pop()


class WindowsProcess:
    """A retained kernel handle identifies the process even after its PID is reused."""

    def __init__(self, pid, allow_terminate=False):
        self.pid = pid
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, returns) in signatures.items():
            fn = getattr(self._api, name)
            fn.argtypes, fn.restype = args, returns
        # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, plus termination
        # rights only when explicitly requested by the caller.
        access = 0x00100000 | 0x1000 | (0x0001 if allow_terminate else 0)
        self._handle = self._api.OpenProcess(access, False, pid)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if not self._api.QueryFullProcessImageNameW(self._handle, 0, path, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            if ntpath.basename(path.value).lower() != "eplan.exe":
                raise RuntimeError("The listening process is not EPLAN.exe.")
            if self.wait(0):
                raise RuntimeError("The target process has already exited.")
        except Exception:
            self.close()
            raise

    def wait(self, seconds):
        status = self._api.WaitForSingleObject(self._handle, min(int(seconds * 1000), 0xFFFFFFFE))
        if status == 0:
            return True
        if status == 258:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def terminate(self):
        if not self._api.TerminateProcess(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self._handle:
            self._api.CloseHandle(self._handle)
            self._handle = None


def connected_process(host, port, allow_terminate=False):
    if str(host).lower() not in {"localhost", "127.0.0.1", "::1", ""}:
        raise RuntimeError("Remote process identity cannot be verified locally.")
    pid = _listener_pid(port)
    process = WindowsProcess(pid, allow_terminate=allow_terminate)
    try:
        if _listener_pid(port) != pid or process.wait(0):
            raise RuntimeError("Port owner changed while identifying the target process.")
    except Exception:
        process.close()
        raise
    return process
