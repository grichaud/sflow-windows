import os
import sys
from dotenv import load_dotenv


def _get_resource_dir() -> str:
    """Read-only bundled assets (logo, etc). PyInstaller puts them in sys._MEIPASS."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def _get_data_dir() -> str:
    """Writable user data (DB, .env). In bundle → %APPDATA%/SFlow on Windows."""
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SFlow")
        return os.path.expanduser("~/Library/Application Support/SFlow")
    return os.path.dirname(os.path.abspath(__file__))


_RESOURCE_DIR = _get_resource_dir()
_DATA_DIR = _get_data_dir()

# Ensure data directory exists when running as bundle
if getattr(sys, "frozen", False):
    os.makedirs(_DATA_DIR, exist_ok=True)

# Load .env from data dir
load_dotenv(os.path.join(_DATA_DIR, ".env"))

# Groq API
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = "whisper-large-v3-turbo"
WHISPER_LANGUAGE = "es"  # Explicit language for accurate accents (é, ó, ñ, etc.)

# Audio
SAMPLE_RATE = 16000
CHANNELS = 1
AUDIO_DTYPE = "int16"
BLOCK_SIZE = 1024

# Preferred input device (microphone).
# SFlow does NOT blindly follow the Windows default input device: plugging in
# headphones can switch the default to a dead/incompatible input (e.g. a
# headphone jack with no working mic), which silently breaks transcription.
# Instead the recorder picks the first input device whose name contains one of
# these hints (case-insensitive) AND that supports 16 kHz mono capture. If none
# match, it falls back to the OS default. Edit/reorder to pin a different mic.
MIC_DEVICE_HINTS = [
    "Microphone Array",   # laptop built-in mic (Intel Smart Sound)
    "Smart Sound",
]

# UI
PILL_WIDTH_IDLE = 34
PILL_WIDTH_RECORDING = 100
PILL_WIDTH_STATUS = 52
PILL_HEIGHT = 34
PILL_OPACITY = 0.90
PILL_CORNER_RADIUS = 17
PILL_MARGIN_BOTTOM = 14
LOGO_SIZE = 22

# Logo path (read-only bundled asset)
LOGO_PATH = os.path.join(_RESOURCE_DIR, "logo_small.png")

# Audio Visualizer
NUM_BARS = 20
VIZ_FPS = 60
BAR_DECAY = 0.85
BAR_GAIN = 8.0

# Hotkey
DOUBLE_TAP_INTERVAL = 0.4  # seconds for double-tap detection

# Database (writable user data)
DB_PATH = os.path.join(_DATA_DIR, "transcriptions.db")

# Exported for other modules
APP_DATA_DIR = _DATA_DIR
