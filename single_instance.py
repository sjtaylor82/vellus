from __future__ import annotations

import ctypes


ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\Vellus-6B0E7C0E-6A0B-4F0C-9B56-3B7D2C4E91A7"


class SingleInstance:
    """Hold a process-wide Windows mutex for the lifetime of the application."""

    def __init__(self, kernel32=None):
        self._kernel32 = kernel32 or ctypes.windll.kernel32
        self._handle = None

    def acquire(self) -> bool:
        self._kernel32.SetLastError(0)
        handle = self._kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            raise ctypes.WinError()
        if self._kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            self._kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

