import sys
import time

_saved_hwnd: int | None = None


if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes

    def save_frontmost_app():
        """Save the currently focused window handle before recording starts."""
        global _saved_hwnd
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd:
                _saved_hwnd = hwnd
        except Exception:
            pass

    def paste_text(text: str):
        """Copy text to clipboard and paste into the previously active window."""
        global _saved_hwnd

        # Copy to clipboard via win32
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
            win32clipboard.CloseClipboard()
        except Exception:
            # Fallback using ctypes directly
            _set_clipboard_ctypes(text)

        # Restore focus to the window that was active before recording
        if _saved_hwnd:
            try:
                ctypes.windll.user32.SetForegroundWindow(_saved_hwnd)
                time.sleep(0.12)
            except Exception:
                pass

        # Simulate Ctrl+V
        _send_ctrl_v()
        _saved_hwnd = None

    def _set_clipboard_ctypes(text: str):
        """Set clipboard text using pure ctypes (no pywin32 dependency)."""
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32

        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        p_mem = kernel32.GlobalLock(h_mem)
        ctypes.memmove(p_mem, encoded, len(encoded))
        kernel32.GlobalUnlock(h_mem)

        user32.OpenClipboard(None)
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_UNICODETEXT, h_mem)
        user32.CloseClipboard()

    def _send_ctrl_v():
        """Send Ctrl+V keypress using SendInput."""
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        VK_CONTROL = 0x11
        VK_V = 0x56

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.wintypes.WORD),
                ("wScan", ctypes.wintypes.WORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.wintypes.DWORD),
                ("ki", KEYBDINPUT),
                ("padding", ctypes.c_ubyte * 8),
            ]

        inputs = (INPUT * 4)()
        # Ctrl down
        inputs[0].type = INPUT_KEYBOARD
        inputs[0].ki.wVk = VK_CONTROL
        # V down
        inputs[1].type = INPUT_KEYBOARD
        inputs[1].ki.wVk = VK_V
        # V up
        inputs[2].type = INPUT_KEYBOARD
        inputs[2].ki.wVk = VK_V
        inputs[2].ki.dwFlags = KEYEVENTF_KEYUP
        # Ctrl up
        inputs[3].type = INPUT_KEYBOARD
        inputs[3].ki.wVk = VK_CONTROL
        inputs[3].ki.dwFlags = KEYEVENTF_KEYUP

        ctypes.windll.user32.SendInput(4, inputs, ctypes.sizeof(INPUT))

else:
    # macOS implementation
    import subprocess

    _saved_app: str | None = None

    def save_frontmost_app():
        """Save the currently focused application before recording starts."""
        global _saved_app
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=2,
            )
            name = result.stdout.strip()
            if name and name != "SFlow":
                _saved_app = name
        except Exception:
            pass

    def paste_text(text: str):
        """Copy text to clipboard and paste into the previously active app."""
        global _saved_app
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString
            pb = NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(text, NSPasteboardTypeString)
        except Exception:
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)

        if _saved_app:
            try:
                subprocess.run(
                    ["osascript", "-e", f'tell application "{_saved_app}" to activate'],
                    check=True, timeout=2,
                )
                time.sleep(0.12)
            except Exception:
                pass

        subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to keystroke "v" using command down'],
            check=True,
        )
        _saved_app = None
