import time
from pynput import keyboard
from PyQt6.QtCore import QObject, pyqtSignal
from config import DOUBLE_TAP_INTERVAL

# On Windows, pressing AltGr is delivered as a *synthetic* Left-Ctrl press
# immediately followed by a RIGHT-Alt press (alt_gr / alt_r). The real record
# shortcut uses the LEFT Alt (alt_l). We discriminate by key identity (left vs
# right Alt), NOT by timing — a fast real Ctrl+LeftAlt is indistinguishable from
# AltGr by timing alone. The window below is used ONLY to neutralize AltGr's
# synthetic Ctrl for double-tap purposes.
ALTGR_WINDOW = 0.06  # seconds


class HotkeyListener(QObject):
    """Global hotkey listener with two modes:

    1. Hold LEFT Ctrl + LEFT Alt: press-and-hold recording.
    2. Double-tap Ctrl: hands-free mode (tap Ctrl again to stop).

    AltGr (Right Alt) is explicitly ignored so it never triggers recording.
    """

    pressed = pyqtSignal()
    released = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._ctrl_held = False
        self._alt_l_held = False
        self._recording = False
        self._hands_free = False
        self._listener: keyboard.Listener | None = None

        # Double-tap detection
        self._last_ctrl_press = 0.0
        self._ctrl_tap_count = 0

        # AltGr disambiguation: timestamp of the last Ctrl press
        self._last_ctrl_press_time = 0.0

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
        now = time.time()

        # AltGr / RIGHT Alt: never a trigger.
        if is_alt_right:
            # Neutralize the synthetic Left-Ctrl that Windows injects right
            # before AltGr, so it can't feed the double-tap detector.
            if now - self._last_ctrl_press_time < ALTGR_WINDOW:
                self._ctrl_held = False
                self._ctrl_tap_count = 0
                self._last_ctrl_press = 0.0
            self._alt_l_held = False
            return

        if is_alt_left:
            self._alt_l_held = True

        elif is_ctrl:
            self._ctrl_held = True
            self._last_ctrl_press_time = now

            # Hands-free: if recording, single Ctrl press stops it
            if self._hands_free and self._recording:
                self._hands_free = False
                self._recording = False
                self.released.emit()
                return

            # Double-tap detection
            if now - self._last_ctrl_press < DOUBLE_TAP_INTERVAL:
                self._ctrl_tap_count += 1
            else:
                self._ctrl_tap_count = 1
            self._last_ctrl_press = now

            if self._ctrl_tap_count >= 2 and not self._recording:
                # Double-tap Ctrl -> hands-free mode
                self._ctrl_tap_count = 0
                self._hands_free = True
                self._recording = True
                self.pressed.emit()
                return

        # Hold mode: LEFT Ctrl + LEFT Alt together
        if self._ctrl_held and self._alt_l_held and not self._recording:
            self._recording = True
            self._hands_free = False
            self.pressed.emit()

    def _on_release(self, key):
        is_ctrl = key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r)
        is_alt = key in (keyboard.Key.alt, keyboard.Key.alt_l,
                         keyboard.Key.alt_r, keyboard.Key.alt_gr)

        if is_ctrl:
            self._ctrl_held = False
        elif is_alt:
            self._alt_l_held = False

        # Hold mode: stop when the combo is broken (but not in hands-free mode)
        if self._recording and not self._hands_free:
            if not (self._ctrl_held and self._alt_l_held):
                self._recording = False
                self.released.emit()
