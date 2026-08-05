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
# whisper-large-v3 ($0.111/hr, 10.3% WER) instead of -turbo ($0.04/hr, 12% WER): the
# turbo variant is the distilled model and it skimps precisely on punctuation and
# capitalization. At ~8 h of audio/month the upgrade costs about $0.55 more per month.
GROQ_MODEL = "whisper-large-v3"
WHISPER_LANGUAGE = "es"  # Explicit language for accurate accents (é, ó, ñ, etc.)

# DISABLED (2026-08-05). Groq's `prompt` param was set to a Spanish style primer to coax
# better punctuation. It backfired badly: Whisper does not merely imitate the prompt's
# style, it BLEEDS ITS CONTENT. Because the primer contained two example questions, short
# and even long dictations came back rewritten as questions that were never spoken
# ("¿Qué puede dormir?" from a 0.6s take; "¿Qué puede durar hasta 30 minutos?" opening a
# 102s take). A prompt also worsens hallucination on near-silent audio.
# If ever re-enabled: declarative sentences ONLY, no questions, no imperative phrasing,
# and verify against real dictations before keeping it. Empty string = param not sent.
WHISPER_PROMPT = ""

# Audio
SAMPLE_RATE = 16000
CHANNELS = 1
AUDIO_DTYPE = "int16"
BLOCK_SIZE = 1024

# Loudest int16 sample (max 32767) below which a take counts as "no sound reached us".
# Sending near-silent audio to Whisper is what makes it hallucinate "Gracias." /
# "Subtitulado por..." and paste it into whatever the user was writing, so such takes
# are dropped before they ever reach the API. 30 is ~0.1% of full scale: far below any
# real speech, including a distant or quiet voice, but above the mic's noise floor.
SILENCE_PEAK_THRESHOLD = 30

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

# Grace period before a broken combo actually ends a dictation.
# This laptop's left Ctrl chatters: while the user holds Ctrl+Alt the driver reports
# genuine (injected=False) key-up/key-down pairs 7-16 ms apart. Without a grace period
# each bounce ended the take and started a new one, shredding one sentence into
# fragments ("pero en estas" / "últimas" / "¿Qué pruebas parece que...") — and Whisper
# invents punctuation, even questions, to make sense of a fragment starting mid-word.
# If the combo is re-formed within this window, the take simply continues; audio keeps
# being captured throughout, so nothing is lost in the gap. The cost is this much extra
# delay before the paste appears, so keep it short.
HOTKEY_RELEASE_GRACE_MS = 250

# Marker stamped into dwExtraInfo of every key event SFlow injects itself (the Ctrl+V
# it sends to paste). The hotkey hook sees injected input just like real typing, so
# without this tag SFlow's own paste re-enters its own shortcut: the Ctrl-up broke the
# combo and cut a dictation already in progress, and the Ctrl-down combined with a still
# held Alt to start phantom recordings that pasted garbage. Any value works as long as
# clipboard.py stamps it and hotkey.py filters on it.
SYNTHETIC_INPUT_TAG = 0x5F10C0DE

# Database (writable user data)
DB_PATH = os.path.join(_DATA_DIR, "transcriptions.db")

# Log file (writable user data). SFlow runs under pythonw.exe, which has no console
# and silently discards stdout/stderr — without this file every crash, API error and
# dropped hotkey is invisible, and "it stopped working" can only be guessed at.
LOG_PATH = os.path.join(_DATA_DIR, "sflow.log")

# Exported for other modules
APP_DATA_DIR = _DATA_DIR
