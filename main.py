#!/usr/bin/env python3
"""SFlow - Voice-to-text desktop tool powered by Groq Whisper."""

import os
import sys
import signal
import subprocess
import threading
from PyQt6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu,
    QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QMessageBox,
)
from PyQt6.QtCore import Qt, QObject, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon, QPixmap, QAction

from ui.pill_widget import PillWidget
from core.recorder import AudioRecorder
from core.transcriber import Transcriber
from core.hotkey import HotkeyListener
from core.clipboard import paste_text, save_frontmost_app
from db.database import TranscriptionDB
from web.server import start_web_server
from config import LOGO_PATH, APP_DATA_DIR, GROQ_API_KEY


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
                exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
                winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, f'"{exe}"')
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

        # MUST use QueuedConnection: pynput emits from its own thread
        self.hotkey.pressed.connect(self._on_hotkey_pressed, Qt.ConnectionType.QueuedConnection)
        self.hotkey.released.connect(self._on_hotkey_released, Qt.ConnectionType.QueuedConnection)
        self.transcription_done.connect(self._on_transcription_done, Qt.ConnectionType.QueuedConnection)
        self.transcription_error.connect(self._on_transcription_error, Qt.ConnectionType.QueuedConnection)

    def start(self):
        self.hotkey.start()
        self.pill.show()
        self.pill.set_state(PillWidget.STATE_IDLE)

    @pyqtSlot()
    def _on_hotkey_pressed(self):
        save_frontmost_app()
        self.recorder.start()
        self.pill.set_state(PillWidget.STATE_RECORDING)

    @pyqtSlot()
    def _on_hotkey_released(self):
        duration = self.recorder.stop()
        self.pill.set_state(PillWidget.STATE_PROCESSING)

        if duration < 0.3:
            self.pill.set_state(PillWidget.STATE_IDLE)
            return

        wav_buffer = self.recorder.get_wav_buffer()
        recording_duration = self.recorder.get_duration()
        thread = threading.Thread(
            target=self._transcribe_worker,
            args=(wav_buffer, recording_duration),
            daemon=True,
        )
        thread.start()

    def _transcribe_worker(self, wav_buffer, duration):
        try:
            text = self.transcriber.transcribe(wav_buffer)
            if text:
                self.transcription_done.emit(text, duration)
            else:
                self.transcription_error.emit("No speech detected")
        except Exception as e:
            self.transcription_error.emit(str(e))

    @pyqtSlot(str, float)
    def _on_transcription_done(self, text: str, duration: float):
        paste_text(text)
        self.db.insert(text=text, duration_seconds=duration)
        self.pill.set_state(PillWidget.STATE_DONE)

    @pyqtSlot(str)
    def _on_transcription_error(self, error: str):
        self.pill.set_state(PillWidget.STATE_ERROR)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
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
