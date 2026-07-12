import io
import wave
import queue
import time
import numpy as np
import sounddevice as sd
from config import SAMPLE_RATE, CHANNELS, AUDIO_DTYPE, BLOCK_SIZE, MIC_DEVICE_HINTS


def resolve_input_device():
    """Pick a stable microphone instead of blindly trusting the OS default.

    Windows can switch the default input device to a dead/incompatible input
    (e.g. a headphone jack with no working mic) when audio hardware is plugged
    in, which silently breaks capture. We look for the first input device whose
    name matches one of MIC_DEVICE_HINTS *and* that supports our capture
    settings (16 kHz mono). The MME host API resamples, so it's the most
    compatible. Returns a device index, or None to fall back to the OS default.
    """
    try:
        devices = sd.query_devices()
    except Exception as e:
        print(f"Could not query audio devices: {e}")
        return None

    def supports(idx: int) -> bool:
        try:
            sd.check_input_settings(
                device=idx,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=AUDIO_DTYPE,
            )
            return True
        except Exception:
            return False

    for hint in MIC_DEVICE_HINTS:
        hint_l = hint.lower()
        matches = [
            i for i, d in enumerate(devices)
            if d["max_input_channels"] > 0 and hint_l in d["name"].lower()
        ]
        for i in matches:
            if supports(i):
                return i

    return None  # no preferred mic available → OS default


def _hostapi_name(index: int) -> str:
    try:
        return sd.query_hostapis(index)["name"]
    except Exception:
        return ""


def _find_input_by_hostapi(hostapi_name: str, hints):
    """First input device on a given host API whose name matches a hint."""
    try:
        devices = sd.query_devices()
    except Exception:
        return None, None
    for i, d in enumerate(devices):
        if d["max_input_channels"] <= 0:
            continue
        if _hostapi_name(d["hostapi"]) != hostapi_name:
            continue
        name = d["name"].lower()
        if any(h.lower() in name for h in hints):
            return i, d
    return None, None


def reset_capture_endpoint() -> bool:
    """Re-initialize the mic's capture pipeline in the Windows audio engine.

    An Intel Smart Sound app (e.g. CallAssist) that opens the mic via WASAPI
    together with a render loopback can leave the endpoint's on-DSP effects
    (AEC/AGC/noise-suppression APO) stuck emitting near-silence for ALL shared
    audio-engine capture — MME, DirectSound and shared-WASAPI — which is what
    SFlow records, so it transcribes nothing (Whisper hallucinates "Gracias.").
    Restarting SFlow does NOT help (same shared engine). Opening the mic via
    WDM-KS (kernel streaming) or WASAPI-exclusive bypasses the shared engine and
    re-arms the endpoint at the driver level — the same thing opening Windows
    "Sound settings" does. We briefly open+close such a stream to clear it.

    Returns True if a reset stream opened. Safe no-op (False) if the device is
    busy/unavailable — normal capture then proceeds regardless.
    """
    attempts = [("Windows WDM-KS", None)]
    try:
        attempts.append(("Windows WASAPI", sd.WasapiSettings(exclusive=True)))
    except Exception:
        pass  # non-Windows / WASAPI unavailable

    for hostapi_name, extra in attempts:
        idx, dev = _find_input_by_hostapi(hostapi_name, MIC_DEVICE_HINTS)
        if idx is None:
            continue
        sr = int(dev["default_samplerate"])  # WDM-KS/exclusive need the native rate, not 16k
        for ch in (1, int(dev["max_input_channels"])):
            if ch < 1:
                continue
            try:
                s = sd.InputStream(
                    device=idx, samplerate=sr, channels=ch, dtype="int16",
                    callback=lambda *a: None,  # WDM-KS requires a callback
                    extra_settings=extra,
                )
                s.start()
                time.sleep(0.05)
                s.stop()
                s.close()
                return True
            except Exception:
                continue
    return False


class AudioRecorder:
    def __init__(self):
        self.audio_queue = queue.Queue()  # For UI visualization
        self.frames: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self.is_recording = False
        self._start_time = 0.0
        self._peak = 0  # loudest sample this recording (to detect a wedged mic)
        # Start "suspect" so the very first take after launch re-arms the mic —
        # handles the common case of restarting SFlow *because* it went silent.
        self._suspect_stuck = True

    def _callback(self, indata: np.ndarray, frames: int, time_info, status):
        if status:
            print(f"Audio status: {status}")
        self.audio_queue.put(indata.copy())
        self.frames.append(indata.copy())
        peak = int(np.abs(indata).max())
        if peak > self._peak:
            self._peak = peak

    def start(self):
        self.frames.clear()
        self._peak = 0
        # Drain any old data from the queue
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        self.is_recording = True
        self._start_time = time.time()
        # If the previous take came back silent, the mic endpoint is likely
        # wedged (an Intel SST / WASAPI app such as CallAssist left it stuck).
        # Re-arm it before capturing so this take isn't silent too.
        if self._suspect_stuck:
            reset_capture_endpoint()
            time.sleep(0.05)
        device = resolve_input_device()
        try:
            self.stream = self._open_stream(device)
        except Exception as e:
            # Chosen device rejected our settings (e.g. sample rate) — fall
            # back to the OS default rather than failing the recording.
            print(f"Failed to open input device {device!r} ({e}); using OS default.")
            self.stream = self._open_stream(None)
        self.stream.start()

    def _open_stream(self, device) -> sd.InputStream:
        return sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=AUDIO_DTYPE,
            blocksize=BLOCK_SIZE,
            device=device,
            callback=self._callback,
        )

    def stop(self) -> float:
        """Stop recording and return duration in seconds."""
        self.is_recording = False
        duration = time.time() - self._start_time
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        # A recording of real length that captured essentially no signal means
        # the mic endpoint is wedged (see reset_capture_endpoint). Flag it so the
        # NEXT take re-arms the endpoint first, self-healing without user action.
        self._suspect_stuck = duration > 0.5 and self._peak < 30
        if self._suspect_stuck:
            print(f"Capture looked stuck (peak={self._peak}); will re-arm mic on next take.")
        return duration

    def get_wav_buffer(self) -> io.BytesIO:
        """Convert recorded frames to in-memory WAV buffer."""
        if not self.frames:
            return io.BytesIO()
        audio_data = np.concatenate(self.frames, axis=0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(audio_data.tobytes())
        buf.seek(0)
        return buf

    def get_duration(self) -> float:
        if not self.frames:
            return 0.0
        total_samples = sum(f.shape[0] for f in self.frames)
        return total_samples / SAMPLE_RATE
