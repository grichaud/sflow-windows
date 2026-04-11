import math
import sys
import ctypes
from PyQt6.QtWidgets import QWidget, QApplication
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QPen, QPixmap
from ui.audio_visualizer import AudioVisualizer
from config import (
    PILL_WIDTH_IDLE,
    PILL_WIDTH_RECORDING,
    PILL_WIDTH_STATUS,
    PILL_HEIGHT,
    PILL_OPACITY,
    PILL_CORNER_RADIUS,
    PILL_MARGIN_BOTTOM,
    LOGO_SIZE,
    LOGO_PATH,
)


class PillWidget(QWidget):
    """Minimal floating pill. Logo + bars when recording, tiny icons for status."""

    STATE_IDLE = "idle"
    STATE_RECORDING = "recording"
    STATE_PROCESSING = "processing"
    STATE_DONE = "done"
    STATE_ERROR = "error"

    def __init__(self):
        super().__init__()
        self._state = self.STATE_IDLE
        self._target_width = PILL_WIDTH_IDLE
        self._current_width = float(PILL_WIDTH_IDLE)
        self._drag_pos = None
        self._bg_color = QColor(15, 15, 15, int(255 * PILL_OPACITY))

        self._logo = QPixmap(LOGO_PATH)
        if not self._logo.isNull():
            self._logo = self._logo.scaled(
                LOGO_SIZE, LOGO_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        self._show_checkmark = False
        self._show_spinner = False
        self._show_error = False
        self._spinner_angle = 0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedHeight(PILL_HEIGHT)
        self.setFixedWidth(PILL_WIDTH_IDLE)

        self.visualizer = AudioVisualizer(parent=self)
        self.visualizer.setVisible(False)

        self._anim_timer = QTimer()
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._animate_width)

        self._spinner_timer = QTimer()
        self._spinner_timer.setInterval(50)
        self._spinner_timer.timeout.connect(self._animate_spinner)

        self._done_timer = QTimer()
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(lambda: self.set_state(self.STATE_IDLE))

        self._position_on_screen()

    def _position_on_screen(self):
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            # Anchor left edge so expansion always goes right
            x = geo.center().x() - PILL_WIDTH_IDLE // 2
            y = geo.bottom() - 4 - PILL_HEIGHT
            self.move(x, y)

    def _setup_native_windows(self):
        """Pin the pill as a topmost tool window that never steals focus."""
        HWND_TOPMOST = -1
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOACTIVATE = 0x0010
        hwnd = int(self.winId())
        ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )

    def _setup_native_macos(self):
        """Configure native macOS window to float above everything without stealing focus."""
        from ctypes import c_void_p
        import AppKit
        import objc
        ns_view = objc.objc_object(c_void_p=c_void_p(self.winId().__int__()))
        ns_window = ns_view.window()
        ns_window.setLevel_(AppKit.NSFloatingWindowLevel)
        ns_window.setStyleMask_(ns_window.styleMask() | AppKit.NSWindowStyleMaskNonactivatingPanel)
        ns_window.setHidesOnDeactivate_(False)
        ns_window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
        )

    def showEvent(self, event):
        """Called when the widget is first shown. Sets up native OS window properties."""
        super().showEvent(event)
        try:
            if sys.platform == "win32":
                self._setup_native_windows()
            else:
                self._setup_native_macos()
        except Exception as e:
            print(f"Warning: native window setup failed: {e}")

    def set_state(self, state: str):
        self._state = state
        self._show_checkmark = False
        self._show_spinner = False
        self._show_error = False
        self._spinner_timer.stop()

        if state == self.STATE_IDLE:
            self._target_width = PILL_WIDTH_IDLE
            self.visualizer.setVisible(False)
            self.visualizer.stop()
        elif state == self.STATE_RECORDING:
            self._target_width = PILL_WIDTH_RECORDING
            self.visualizer.setVisible(True)
            self.visualizer.start()
        elif state == self.STATE_PROCESSING:
            self._target_width = PILL_WIDTH_STATUS
            self._show_spinner = True
            self._spinner_timer.start()
            self.visualizer.setVisible(False)
            self.visualizer.stop()
        elif state == self.STATE_DONE:
            self._target_width = PILL_WIDTH_STATUS
            self._show_checkmark = True
            self.visualizer.setVisible(False)
            self.visualizer.stop()
            self._done_timer.start(800)
        elif state == self.STATE_ERROR:
            self._target_width = PILL_WIDTH_STATUS
            self._show_error = True
            self.visualizer.setVisible(False)
            self.visualizer.stop()
            self._done_timer.start(1200)

        if not self._anim_timer.isActive():
            self._anim_timer.start()
        self.update()

    def _animate_spinner(self):
        self._spinner_angle = (self._spinner_angle + 30) % 360
        self.update()

    def _animate_width(self):
        diff = self._target_width - self._current_width
        if abs(diff) < 1:
            self._current_width = float(self._target_width)
            self._anim_timer.stop()
        else:
            self._current_width += diff * 0.22

        # Anchor left edge: logo stays fixed, expansion goes right
        left_x = self.x()
        self.setFixedWidth(int(self._current_width))
        self.move(left_x, self.y())
        self._layout_children()
        self.update()

    def _layout_children(self):
        w = int(self._current_width)
        h = PILL_HEIGHT
        logo_pad = 6
        logo_area = logo_pad + LOGO_SIZE + 4
        content_w = w - logo_area - 4
        if content_w > 0 and self.visualizer.isVisible():
            self.visualizer.setGeometry(logo_area, 2, content_w, h - 4)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()

        # Background
        path = QPainterPath()
        path.addRoundedRect(0.0, 0.0, float(w), float(h), PILL_CORNER_RADIUS, PILL_CORNER_RADIUS)
        painter.fillPath(path, self._bg_color)

        # Border
        painter.setPen(QPen(QColor(255, 255, 255, 12), 0.5))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(0, 0, w, h, PILL_CORNER_RADIUS, PILL_CORNER_RADIUS)

        # Logo
        if not self._logo.isNull():
            lx = 6
            ly = (h - LOGO_SIZE) // 2
            painter.drawPixmap(lx, ly, self._logo)

        # Status icons - positioned right of logo, centered in remaining space
        icon_cx = 6 + LOGO_SIZE + 4 + (w - 6 - LOGO_SIZE - 4 - 4) // 2
        icon_cy = h // 2

        if self._show_checkmark:
            pen = QPen(QColor(80, 210, 120), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawLine(icon_cx - 4, icon_cy, icon_cx - 1, icon_cy + 3)
            painter.drawLine(icon_cx - 1, icon_cy + 3, icon_cx + 5, icon_cy - 3)

        elif self._show_spinner:
            painter.setPen(Qt.PenStyle.NoPen)
            for i in range(6):
                angle = math.radians(self._spinner_angle + i * 60)
                dx = 5 * math.cos(angle)
                dy = 5 * math.sin(angle)
                alpha = 220 - i * 35
                painter.setBrush(QColor(255, 255, 255, max(alpha, 30)))
                s = 2
                painter.drawEllipse(int(icon_cx + dx) - 1, int(icon_cy + dy) - 1, s, s)

        elif self._show_error:
            pen = QPen(QColor(255, 70, 70), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(icon_cx - 3, icon_cy - 3, icon_cx + 3, icon_cy + 3)
            painter.drawLine(icon_cx - 3, icon_cy + 3, icon_cx + 3, icon_cy - 3)

        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
