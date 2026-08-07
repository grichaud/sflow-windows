#!/usr/bin/env python3
"""SFlow - Voice-to-text desktop tool powered by Groq Whisper."""

import os
import sys
import time
import signal
import subprocess
import threading
from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu,
    QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox,
)
from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon, QPixmap, QAction

from core.log import setup_logging, get_logger
from ui.pill_widget import PillWidget
from core.recorder import AudioRecorder
from core.transcriber import Transcriber
from core.hotkey import HotkeyListener
from core.clipboard import paste_text, save_frontmost_app
from db.database import TranscriptionDB
from web.server import start_web_server
from config import (
    LOGO_PATH, APP_DATA_DIR, GROQ_API_KEY, GROQ_MODEL, WHISPER_LANGUAGE,
    SILENCE_PEAK_THRESHOLD, HOTKEY_RELEASE_GRACE_MS,
    HOTKEY_RELEASE_GRACE_CHATTER_MS, CHATTER_WINDOW_SECONDS, CLIP_WARN_PERCENT,
)

setup_logging()
log = get_logger("sflow.app")


def _ensure_accessibility() -> bool:
    """On macOS: prompt for Accessibility permission. On Windows: no-op."""
    if sys.platform != "darwin":
        return True
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions
        return AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Launch at login
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    import winreg
    _RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _APP_NAME = "SFlow"

    def _is_launch_at_login() -> bool:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, _APP_NAME)
                return True
        except FileNotFoundError:
            return False

    def _set_launch_at_login(enabled: bool):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    # Dev mode: launch the script with the venv's windowed interpreter
                    # (pythonw.exe -> no console window at boot). Running the bare .py
                    # would use the system file association (wrong Python, no venv).
                    exe_dir = os.path.dirname(sys.executable)
                    pythonw = os.path.join(exe_dir, "pythonw.exe")
                    interp = pythonw if os.path.exists(pythonw) else sys.executable
                    cmd = f'"{interp}" "{os.path.abspath(sys.argv[0])}"'
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, _APP_NAME)
                except FileNotFoundError:
                    pass

else:
    # macOS LaunchAgent
    _LAUNCH_AGENT_LABEL = "so.saasfactory.sflow"
    _PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{_LAUNCH_AGENT_LABEL}.plist")

    def _is_launch_at_login() -> bool:
        return os.path.exists(_PLIST_PATH)

    def _set_launch_at_login(enabled: bool):
        if enabled:
            exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
            plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>"""
            os.makedirs(os.path.dirname(_PLIST_PATH), exist_ok=True)
            with open(_PLIST_PATH, "w") as f:
                f.write(plist)
            subprocess.run(["launchctl", "load", _PLIST_PATH], capture_output=True)
        else:
            if os.path.exists(_PLIST_PATH):
                subprocess.run(["launchctl", "unload", _PLIST_PATH], capture_output=True)
                os.remove(_PLIST_PATH)


# ---------------------------------------------------------------------------
# First-run dialog
# ---------------------------------------------------------------------------
class FirstRunDialog(QDialog):
    """Shown when GROQ_API_KEY is missing on first launch."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SFlow - Setup")
        self.setFixedWidth(420)

        layout = QVBoxLayout()
        layout.addWidget(QLabel("Ingresa tu Groq API Key para transcripciones:"))

        link = QLabel('<a href="https://console.groq.com/keys">Obtener gratis en console.groq.com/keys</a>')
        link.setOpenExternalLinks(True)
        layout.addWidget(link)

        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("gsk_...")
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.key_input)

        save_btn = QPushButton("Guardar y continuar")
        save_btn.clicked.connect(self._save_key)
        layout.addWidget(save_btn)

        self.setLayout(layout)

    def _save_key(self):
        key = self.key_input.text().strip()
        if not key.startswith("gsk_") or len(key) < 20:
            QMessageBox.warning(self, "Error", "La clave debe comenzar con 'gsk_' y tener al menos 20 caracteres.")
            return

        env_path = os.path.join(APP_DATA_DIR, ".env")
        os.makedirs(APP_DATA_DIR, exist_ok=True)
        with open(env_path, "w") as f:
            f.write(f"GROQ_API_KEY={key}\n")

        # Set in current process so Transcriber picks it up
        os.environ["GROQ_API_KEY"] = key
        self.accept()


# ---------------------------------------------------------------------------
# System tray
# ---------------------------------------------------------------------------
def _setup_tray(app: QApplication, port: int) -> QSystemTrayIcon:
    pixmap = QPixmap(LOGO_PATH)
    if pixmap.isNull():
        # Fallback: empty icon (shouldn't happen but don't crash)
        icon = QIcon()
    else:
        icon = QIcon(pixmap.scaled(22, 22, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    tray = QSystemTrayIcon(icon, app)

    menu = QMenu()

    status = QAction("SFlow - Activo", menu)
    status.setEnabled(False)
    menu.addAction(status)
    menu.addSeparator()

    import webbrowser
    dashboard = QAction(f"Abrir Dashboard (:{port})", menu)
    dashboard.triggered.connect(lambda: webbrowser.open(f"http://localhost:{port}"))
    menu.addAction(dashboard)
    menu.addSeparator()

    login_label = "Iniciar con Windows" if sys.platform == "win32" else "Iniciar con macOS"
    login_action = QAction(login_label, menu)
    login_action.setCheckable(True)
    login_action.setChecked(_is_launch_at_login())
    login_action.toggled.connect(_set_launch_at_login)
    menu.addAction(login_action)
    menu.addSeparator()

    quit_action = QAction("Salir", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.setToolTip("SFlow - Voice to Text")
    tray.show()
    return tray


# ---------------------------------------------------------------------------
# Main app controller
# ---------------------------------------------------------------------------
_MAX_RECORD_SECONDS = 300  # hard cap (5 min) so a stuck recording can't run forever;
# generous enough for long dictations — a shorter cap cut real dictations off.


class SFlowApp(QObject):
    """Main application controller. Wires hotkey -> recorder -> transcriber -> clipboard."""

    transcription_done = pyqtSignal(str, float)
    transcription_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.recorder = AudioRecorder()
        self.transcriber = Transcriber()
        self.db = TranscriptionDB()
        self.hotkey = HotkeyListener()
        self.pill = PillWidget()

        # Connect visualizer to recorder's audio queue
        self.pill.visualizer.set_audio_queue(self.recorder.audio_queue)

        # Watchdog: while recording, poll the REAL keyboard state so a dropped
        # key-release event can't leave SFlow stuck listening (and enforce a max
        # duration). Runs on the main thread (QTimer parented to this QObject).
        self._record_start = 0.0
        self._hold_watchdog = QTimer(self)
        self._hold_watchdog.setInterval(350)
        self._hold_watchdog.timeout.connect(self._check_hold)

        # Debounce for a chattering Ctrl key — see HOTKEY_RELEASE_GRACE_MS.
        self._release_grace = QTimer(self)
        self._release_grace.setSingleShot(True)
        self._release_grace.timeout.connect(self._finish_recording)
        self._chatter_recoveries = 0
        self._last_bounce_at = 0.0

        # Health heartbeat — see _beat().
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(60_000)
        self._heartbeat.timeout.connect(self._beat)

        # MUST use QueuedConnection: pynput emits from its own thread
        self.hotkey.pressed.connect(self._on_hotkey_pressed, Qt.ConnectionType.QueuedConnection)
        self.hotkey.released.connect(self._on_hotkey_released, Qt.ConnectionType.QueuedConnection)
        self.transcription_done.connect(self._on_transcription_done, Qt.ConnectionType.QueuedConnection)
        self.transcription_error.connect(self._on_transcription_error, Qt.ConnectionType.QueuedConnection)

    def start(self):
        self.hotkey.start()
        self.pill.show()
        self.pill.set_state(PillWidget.STATE_IDLE)
        log.info("SFlow started | model=%s language=%s", GROQ_MODEL, WHISPER_LANGUAGE)
        self._heartbeat.start()
        # Open the mic now, off the UI thread, so the first dictation is as fast as
        # the rest (opening costs ~700 ms — see AudioRecorder._ensure_stream).
        threading.Thread(target=self._prewarm_mic, daemon=True).start()

    def _prewarm_mic(self):
        started = time.time()
        self.recorder.prewarm()
        log.info("mic prewarmed in %.0f ms", (time.time() - started) * 1000)

    def _beat(self):
        """Periodic health line, so a silent failure leaves a trace in the log.

        Also revives the hotkey listener if its thread died — otherwise the shortcut
        is dead until SFlow is restarted by hand, with nothing explaining why.
        """
        log.info(
            "heartbeat | recording=%s key_events=%d last_key=%.0fs listener_alive=%s "
            "self_keys_filtered=%d key_bounces_absorbed=%d",
            self.recorder.is_recording,
            self.hotkey.events_seen(),
            self.hotkey.seconds_since_last_event(),
            self.hotkey.is_alive(),
            self.hotkey.self_events_filtered(),
            self._chatter_recoveries,
        )
        if not self.hotkey.is_alive():
            log.error("hotkey listener thread is dead — reviving")
            self.hotkey.restart()

    @pyqtSlot()
    def _on_hotkey_pressed(self):
        # The combo came back while we were waiting out a possible key bounce, so this
        # was never a real release: keep the take running instead of chopping the
        # sentence in two. Audio kept being captured throughout the gap.
        if self._release_grace.isActive():
            self._release_grace.stop()
            self._chatter_recoveries += 1
            self._last_bounce_at = time.time()
            log.info("combo re-formed within grace — continuing take (bounce #%d)",
                     self._chatter_recoveries)
            return

        # Never let an exception escape this slot: it would leave the recorder
        # flagged as recording with no stream, and the pill stuck out of sync.
        try:
            save_frontmost_app()
            self.recorder.start()
        except Exception:
            log.exception("failed to start recording")
            self.hotkey.force_release()
            self.recorder.is_recording = False
            self.pill.set_state(PillWidget.STATE_ERROR)
            return
        self.pill.set_state(PillWidget.STATE_RECORDING)
        self._record_start = time.time()
        self._hold_watchdog.start()

    def _check_hold(self):
        """Safety cap only: force-stop a recording that has run too long (e.g. a
        key-release was dropped and it's stuck 'on'). We do NOT poll the physical
        key state — GetAsyncKeyState proved unreliable here and cut off speech
        mid-sentence; pynput's release event is what normally stops recording."""
        if not self.recorder.is_recording:
            self._hold_watchdog.stop()
            return
        if time.time() - self._record_start > _MAX_RECORD_SECONDS:
            log.warning("max duration reached (%ds) — force-stopping", _MAX_RECORD_SECONDS)
            self.hotkey.force_release()   # reset hotkey state so the next press works
            self._finish_recording()      # no grace period: this is a hard stop

    @pyqtSlot()
    def _on_hotkey_released(self):
        if not self.recorder.is_recording:
            return  # already stopped (watchdog + real release, or a double event)
        # Don't end the take yet — a chattering Ctrl produces a release followed by a
        # press milliseconds later. Recording continues during the grace period, so if
        # the combo comes back nothing is lost; otherwise _finish_recording() runs.
        self._release_grace.start(self._grace_ms())

    def _grace_ms(self) -> int:
        """Wait longer while the keyboard is actively chattering.

        Bounces come in bursts, so once one is seen we widen the window to cover the
        slow stragglers (up to ~1.4 s were measured) and let it relax back afterwards.
        This keeps normal dictations from paying that delay on every paste.
        """
        if time.time() - self._last_bounce_at < CHATTER_WINDOW_SECONDS:
            return HOTKEY_RELEASE_GRACE_CHATTER_MS
        return HOTKEY_RELEASE_GRACE_MS

    def _finish_recording(self):
        self._hold_watchdog.stop()
        self._release_grace.stop()
        if not self.recorder.is_recording:
            return
        duration = self.recorder.stop()
        self.pill.set_state(PillWidget.STATE_PROCESSING)

        if duration < 0.3:
            self.pill.set_state(PillWidget.STATE_IDLE)
            return

        # Never send silence to Whisper. It does not return "nothing" for silent audio —
        # it hallucinates a plausible phrase ("Gracias.", "Subtitulado por...") which then
        # gets pasted into whatever the user was writing. A take with no frames, or whose
        # loudest sample is below the noise floor, means the mic gave us nothing (a dead
        # endpoint, or another instance holding the device), so we surface an error
        # instead of inventing text.
        if not self.recorder.captured_sound():
            log.warning(
                "discarding silent take: %.1fs held, %d frames, peak=%d (threshold %d) — "
                "mic captured nothing, not sending to Whisper",
                duration, len(self.recorder.frames), self.recorder.peak,
                SILENCE_PEAK_THRESHOLD,
            )
            self.pill.set_state(PillWidget.STATE_ERROR)
            return

        # Record the input level of every take. Clipping cannot be undone in software,
        # so this is the only way to tell "the transcription is bad" apart from "the mic
        # level in Windows is too high and the audio arrived already destroyed".
        clip = self.recorder.clip_percent
        if clip >= CLIP_WARN_PERCENT:
            log.warning(
                "INPUT TOO LOUD: %.1f%% of this take is clipped (peak=%d/32767). "
                "Speech is distorted before SFlow receives it — lower the microphone "
                "level in Windows Sound settings.", clip, self.recorder.peak,
            )
        else:
            log.info("level ok | peak=%d/32767 clipped=%.2f%%", self.recorder.peak, clip)

        wav_buffer = self.recorder.get_wav_buffer()
        recording_duration = self.recorder.get_duration()
        thread = threading.Thread(
            target=self._transcribe_worker,
            args=(wav_buffer, recording_duration),
            daemon=True,
        )
        thread.start()

    def _transcribe_worker(self, wav_buffer, duration):
        started = time.time()
        try:
            text = self.transcriber.transcribe(wav_buffer)
            elapsed = time.time() - started
            if text:
                log.info("transcribed %.1fs audio in %.1fs | %d chars",
                         duration, elapsed, len(text))
                self.transcription_done.emit(text, duration)
            else:
                log.warning("empty transcription for %.1fs audio (%.1fs)", duration, elapsed)
                self.transcription_error.emit("No speech detected")
        except Exception as e:
            # The API call is the single most likely thing to fail silently (expired
            # key, VPN blocking Groq, 10s timeout). Record the type, not just str(e).
            log.exception("transcription FAILED after %.1fs for %.1fs audio",
                          time.time() - started, duration)
            self.transcription_error.emit(f"{type(e).__name__}: {e}")

    @pyqtSlot(str, float)
    def _on_transcription_done(self, text: str, duration: float):
        # Save to the DB even if pasting fails, so a good transcription is never lost
        # to a locked clipboard — and so the pill can't get stranded on PROCESSING.
        try:
            paste_text(text)
        except Exception:
            log.exception("paste failed; text kept in history only")
        try:
            # Record the model/language actually used — the table's default is a stale
            # hardcoded 'whisper-large-v3-turbo', which would make history unusable for
            # comparing transcription quality across model changes.
            self.db.insert(
                text=text,
                language=WHISPER_LANGUAGE,
                duration_seconds=duration,
                model=GROQ_MODEL,
            )
        except Exception:
            log.exception("db insert failed")
        self.pill.set_state(PillWidget.STATE_DONE)

    @pyqtSlot(str)
    def _on_transcription_error(self, error: str):
        log.warning("transcription error surfaced to user: %s", error)
        self.pill.set_state(PillWidget.STATE_ERROR)


# ---------------------------------------------------------------------------
# Single-instance guard
# ---------------------------------------------------------------------------
# SFlow is a global-hotkey tray app: two copies fight over the hotkey and the
# microphone (each hotkey press opens a competing audio stream), which makes
# recording flaky or silent. This venv is based on Anaconda, and the venv
# interpreter can end up re-launching a second copy under the Anaconda base
# python — so we defensively refuse to run more than one instance at a time.
_INSTANCE_LOCK = None  # keep the handle/socket alive for the process lifetime


def _acquire_single_instance() -> bool:
    """Return True if this is the only running instance, False otherwise.

    Note: with this Anaconda-based venv, `pythonw.exe` is a redirect launcher
    that re-execs the base interpreter, so SFlow normally shows up as two OS
    processes (launcher stub + real interpreter). Only the real interpreter
    runs this code, so the guard still sees exactly one instance.
    """
    global _INSTANCE_LOCK
    if sys.platform == "win32":
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        # use_last_error=True is REQUIRED: with plain ctypes.windll, GetLastError() is
        # read through a separate foreign call that can clobber the very error code we
        # are testing, so the "already running" case was missed and a second copy
        # started — two instances then fought over the mic and one recorded pure
        # silence, which Whisper turned into "Gracias.".
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, True, "Local\\SFlow_SingleInstance")
        err = ctypes.get_last_error()
        if not handle or err == ERROR_ALREADY_EXISTS:
            return False
        _INSTANCE_LOCK = handle
        return True
    # macOS/Linux: hold a fixed loopback port for the process lifetime.
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 49321))
    except OSError:
        s.close()
        return False
    s.listen(1)
    _INSTANCE_LOCK = s
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    # Refuse to start a duplicate (must run BEFORE anything grabs the mic/hotkey).
    if not _acquire_single_instance():
        log.warning("another SFlow instance is already running — exiting (pid %d)",
                    os.getpid())
        sys.exit(0)

    app = QApplication(sys.argv)
    app.setApplicationName("SFlow")
    app.setQuitOnLastWindowClosed(False)

    # Allow Ctrl+C to kill the app
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # First-run: ask for API key if missing (BEFORE hiding from Dock so dialog is visible)
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        dialog = FirstRunDialog()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            sys.exit(0)

    # Hide from Dock on macOS (menu bar only) — not needed on Windows
    if sys.platform == "darwin":
        try:
            import AppKit
            AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        except Exception:
            pass

    # Start web dashboard
    port = start_web_server()

    # Request Accessibility permission (shows macOS prompt if not granted)
    _ensure_accessibility()

    # Start the app
    sflow = SFlowApp()
    sflow.start()

    # System tray icon
    tray = _setup_tray(app, port)  # noqa: F841 — must keep reference alive

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
