"""
Input Locker – blocks ALL keyboard and mouse input system-wide on Windows
using the BlockInput() Win32 API (requires Administrator).

BlockInput(True)  → blocks all keyboard + mouse input (including Win key)
BlockInput(False) → restores input

The ONLY key combination Windows always allows through is Ctrl+Alt+Del
(enforced by the kernel, cannot be overridden by any user-mode code).

An ESC override is provided via GetAsyncKeyState polling so the operator
can dismiss the lock without Ctrl+Alt+Del.

Usage:
    locker = InputLocker()
    locker.lock()    # blocks all input
    locker.unlock()  # restores input
"""

import ctypes
import ctypes.wintypes
import threading
import time

user32 = ctypes.windll.user32

VK_ESCAPE = 0x1B


class InputLocker:
    """
    Blocks all keyboard and mouse input using BlockInput().
    Thread-safe: lock()/unlock() can be called from any thread.
    """

    def __init__(self, on_esc_callback=None):
        """
        on_esc_callback: optional callable invoked when ESC is pressed
                         while input is locked (polled via GetAsyncKeyState).
        """
        self._locked = False
        self._failed = False
        self._lock = threading.Lock()
        self._on_esc = on_esc_callback
        self._monitor_thread = None
        self._stop_event = threading.Event()

    @property
    def is_locked(self):
        return self._locked

    def lock(self):
        """Block all keyboard and mouse input. Requires Administrator."""
        with self._lock:
            if self._locked or self._failed:
                return
            result = user32.BlockInput(True)
            if not result:
                err = ctypes.get_last_error() or ctypes.windll.kernel32.GetLastError()
                print(f"[LOCK] WARNING: BlockInput failed (error {err}). "
                      "Are you running as Administrator?")
                self._failed = True
                return
            self._locked = True
            self._stop_event.clear()

        # Start ESC-polling monitor thread
        self._monitor_thread = threading.Thread(
            target=self._esc_monitor, daemon=True)
        self._monitor_thread.start()
        print("[LOCK] All input BLOCKED (Ctrl+Alt+Del or ESC to unlock)")

    def unlock(self):
        """Restore all keyboard and mouse input."""
        with self._lock:
            if not self._locked:
                return
            user32.BlockInput(False)
            self._locked = False

        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2.0)
        self._monitor_thread = None
        print("[LOCK] Input UNBLOCKED")

    def _esc_monitor(self):
        """
        Poll GetAsyncKeyState(VK_ESCAPE) to detect ESC while input is
        blocked.  GetAsyncKeyState reads the hardware key state directly
        and works even when BlockInput is active (because we are the
        calling process – BlockInput doesn't block the calling thread).
        """
        while not self._stop_event.is_set():
            # Bit 0x8000 = key is currently down
            state = user32.GetAsyncKeyState(VK_ESCAPE)
            if state & 0x8000:
                if self._on_esc:
                    # Fire callback (which should call unlock)
                    self._on_esc()
                break
            time.sleep(0.05)  # 50 ms poll interval
