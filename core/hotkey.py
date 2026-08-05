import logging
import sys
import time

from pynput import keyboard
from PyQt6.QtCore import QObject, pyqtSignal

from config import SYNTHETIC_INPUT_TAG
from core.log import get_logger

log = get_logger("sflow.hotkey")


# Modifier virtual-key codes we care about, and the messages that carry them.
_VK_NAMES = {0x10: "SHIFT", 0x11: "CTRL", 0x12: "ALT",
             0xA0: "LSHIFT", 0xA1: "RSHIFT", 0xA2: "LCTRL", 0xA3: "RCTRL",
             0xA4: "LALT", 0xA5: "RALT/AltGr"}
_MSG_NAMES = {0x100: "down", 0x101: "up", 0x104: "down(sys)", 0x105: "up(sys)"}
_LLKHF_INJECTED = 0x10


def _make_win32_filter(counter):
    """Drop the key events SFlow itself injects (the paste's Ctrl+V).

    pynput's low-level hook receives injected input exactly like real typing, so
    without this the paste's Ctrl re-entered our own shortcut. We match on the
    dwExtraInfo marker clipboard.py stamps, which is precise — unlike filtering on
    the generic LLKHF_INJECTED flag, this ignores only OUR keys and still honors
    other automation tools the user may run.
    """
    def win32_event_filter(msg, data):
        if getattr(data, "dwExtraInfo", 0) == SYNTHETIC_INPUT_TAG:
            counter()
            return False  # don't deliver to on_press/on_release (still reaches the OS)
        # Trace only the modifier keys of our shortcut. Logging every keystroke would
        # both slow the hook (Windows silently unhooks a slow one) and record what the
        # user types. `injected` tells apart the user's own keyboard from software —
        # another tool synthesising Ctrl/Alt would break the combo mid-dictation.
        vk = getattr(data, "vkCode", 0)
        if vk in _VK_NAMES and log.isEnabledFor(logging.DEBUG):
            log.debug(
                "key %-10s %-9s injected=%s",
                _VK_NAMES[vk], _MSG_NAMES.get(msg, msg),
                bool(getattr(data, "flags", 0) & _LLKHF_INJECTED),
            )
        return True
    return win32_event_filter


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
        # Liveness telemetry. Windows silently unhooks a WH_KEYBOARD_LL hook that it
        # considers unresponsive; when that happens pynput's thread stays alive but
        # stops receiving events, so the hotkey dies with no error anywhere. These
        # let the heartbeat prove whether events are still arriving.
        self._events_seen = 0
        self._last_event_at = time.monotonic()
        self._self_events_filtered = 0

    def _count_self_event(self):
        self._self_events_filtered += 1

    def start(self):
        kwargs = {}
        if sys.platform == "win32":
            kwargs["win32_event_filter"] = _make_win32_filter(self._count_self_event)
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            **kwargs,
        )
        self._listener.daemon = True
        self._listener.start()
        log.info("hotkey listener started (self-input filter=%s)", bool(kwargs))

    def is_alive(self) -> bool:
        return bool(self._listener and self._listener.is_alive())

    def seconds_since_last_event(self) -> float:
        return time.monotonic() - self._last_event_at

    def events_seen(self) -> int:
        return self._events_seen

    def self_events_filtered(self) -> int:
        return self._self_events_filtered

    def restart(self):
        """Re-create the listener (and its OS hook) after it has gone deaf/dead."""
        log.warning("restarting hotkey listener")
        try:
            self.stop()
        except Exception:
            log.exception("error stopping dead listener")
        self._ctrl_held = False
        self._alt_l_held = False
        self._recording = False
        self._last_event_at = time.monotonic()
        self.start()

    def stop(self):
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key):
        self._events_seen += 1
        self._last_event_at = time.monotonic()
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
            log.info("combo pressed -> start")
            self.pressed.emit()

    def _on_release(self, key):
        self._events_seen += 1
        self._last_event_at = time.monotonic()
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
            log.info("combo released -> stop")
            self.released.emit()

    def force_release(self):
        """Reset key/recording state without emitting (used by the watchdog
        when a take ran too long — the caller drives the stop path). Ensures the
        next press works cleanly."""
        self._ctrl_held = False
        self._alt_l_held = False
        self._recording = False
