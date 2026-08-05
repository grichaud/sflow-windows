from pynput import keyboard
from PyQt6.QtCore import QObject, pyqtSignal


class HotkeyListener(QObject):
    """Global hotkey: hold LEFT Ctrl + LEFT Alt to record (press and hold).

    Right Alt / AltGr is ignored so typing @ { [ (AltGr on a Spanish keyboard)
    never triggers recording.

    There is NO double-tap / hands-free mode. It used to exist (double-tap Ctrl
    to toggle recording) but it collided with normal Ctrl usage — Ctrl+C then
    Ctrl+V within 0.4 s, or just holding Ctrl (which auto-repeats), looked like a
    "double tap" and started a hidden recording that pasted "Gracias." and left
    SFlow stuck listening (with `_recording` stuck True, plain Ctrl+Alt then did
    nothing until SFlow was restarted).
    """

    pressed = pyqtSignal()
    released = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._ctrl_held = False
        self._alt_l_held = False
        self._recording = False
        self._listener: keyboard.Listener | None = None

    def start(self):
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self):
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key):
        is_ctrl = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_alt_left = key in (keyboard.Key.alt_l, keyboard.Key.alt)
        is_alt_right = key in (keyboard.Key.alt_r, keyboard.Key.alt_gr)

        # Right Alt / AltGr is never part of the shortcut (and injects a
        # synthetic Left-Ctrl on Windows) — clear left-alt state and bail.
        if is_alt_right:
            self._alt_l_held = False
            return

        if is_ctrl:
            self._ctrl_held = True
        elif is_alt_left:
            self._alt_l_held = True

        # Hold mode: LEFT Ctrl + LEFT Alt held together.
        if self._ctrl_held and self._alt_l_held and not self._recording:
            self._recording = True
            self.pressed.emit()

    def _on_release(self, key):
        is_ctrl = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_alt = key in (keyboard.Key.alt, keyboard.Key.alt_l,
                         keyboard.Key.alt_r, keyboard.Key.alt_gr)

        if is_ctrl:
            self._ctrl_held = False
        elif is_alt:
            self._alt_l_held = False

        # Stop as soon as the combo is broken.
        if self._recording and not (self._ctrl_held and self._alt_l_held):
            self._recording = False
            self.released.emit()

    def force_release(self):
        """Reset key/recording state without emitting (used by the watchdog
        when a take ran too long — the caller drives the stop path). Ensures the
        next press works cleanly."""
        self._ctrl_held = False
        self._alt_l_held = False
        self._recording = False
