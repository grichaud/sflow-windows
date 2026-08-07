import io
import wave
import queue
import time
import numpy as np
import sounddevice as sd
from config import (
    SAMPLE_RATE, CHANNELS, AUDIO_DTYPE, BLOCK_SIZE, MIC_DEVICE_HINTS,
    SILENCE_PEAK_THRESHOLD, CLIP_LEVEL,
)


def _supports(idx: int) -> bool:
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


def _pinned_device(devices):
    """First input matching MIC_DEVICE_HINTS that can do 16 kHz mono, else None."""
    for hint in MIC_DEVICE_HINTS:
        hint_l = hint.lower()
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0 and hint_l in d["name"].lower() and _supports(i):
                return i
    return None


def resolve_input_device(prefer_pinned: bool = False):
    """Pick the capture device, honoring the user's Windows default input.

    The default is what the user deliberately chooses when they plug in a headset, so
    following it is the behaviour they expect — SFlow used to ignore it entirely and
    record the built-in array while the user spoke into a headset boom mic.

    The reason it ignored it still exists though: plugging headphones into the 3.5 mm
    jack can make Windows select a jack input with no working mic, and capture goes
    silently dead. So MIC_DEVICE_HINTS remains as a fallback, used when the default
    can't do 16 kHz mono, or when `prefer_pinned` is set because a take came back
    silent on the default. Returns a device index, or None for "let PortAudio decide".
    """
    try:
        devices = sd.query_devices()
    except Exception as e:
        print(f"Could not query audio devices: {e}")
        return None

    if not prefer_pinned:
        try:
            default_idx = sd.default.device[0]
        except Exception:
            default_idx = None
        if (isinstance(default_idx, int) and 0 <= default_idx < len(devices)
                and devices[default_idx]["max_input_channels"] > 0
                and _supports(default_idx)):
            return default_idx

    return _pinned_device(devices)


def device_name(idx) -> str:
    if idx is None:
        return "(PortAudio default)"
    try:
        return sd.query_devices(idx)["name"]
    except Exception:
        return f"device {idx}"


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
        self._clipped = 0        # samples pinned at full scale (input level too high)
        self._total_samples = 0
        # Only re-arm the endpoint AFTER a take actually comes back silent — a
        # proactive warm-up on the first take added latency and could leave a
        # sliver of leading silence that Whisper transcribes as "Gracias.".
        self._suspect_stuck = False
        # Set once the Windows default input proves it captures nothing, so we switch
        # to MIC_DEVICE_HINTS (the dead-3.5mm-jack case) instead of staying deaf.
        self._prefer_pinned = False

    def _callback(self, indata: np.ndarray, frames: int, time_info, status):
        if status:
            print(f"Audio status: {status}")
        if not self.is_recording:
            return  # stream is kept open between takes; ignore anything outside a take
        self.audio_queue.put(indata.copy())
        self.frames.append(indata.copy())
        mag = np.abs(indata)
        peak = int(mag.max())
        if peak > self._peak:
            self._peak = peak
        # Count samples pinned at (near) full scale. Once the input level is high enough
        # to clip, the waveform is squared off and the information is destroyed before
        # we ever see it — no amount of processing recovers it, and Whisper transcribes
        # clipped speech badly or drops it. Only lowering the mic level in Windows helps.
        self._clipped += int((mag >= CLIP_LEVEL).sum())
        self._total_samples += mag.size

    def _ensure_stream(self):
        """Open the capture stream if it isn't already. Kept open across takes.

        Opening a stream costs ~700 ms on this machine (MME; DirectSound is no better),
        and it used to happen inside the hotkey handler on the UI thread for every
        single take. That froze the app for ~1 s per dictation: the first second of
        speech was never captured (so short takes reached Whisper near-empty and came
        back as "Gracias.") and the user's key presses queued up behind the freeze and
        were applied to the wrong take, which looked like recording "cutting out".
        Starting an already-open stream costs 0.4 ms instead.
        """
        if self.stream is not None:
            return
        device = resolve_input_device(self._prefer_pinned)
        try:
            self.stream = self._open_stream(device)
        except Exception as e:
            # Chosen device rejected our settings (e.g. sample rate) — fall back to
            # the OS default rather than failing the recording.
            print(f"Failed to open input device {device!r} ({e}); using OS default.")
            self.stream = self._open_stream(None)
            device = None
        print(f"Mic open: {device_name(device)}"
              f"{' [pinned fallback]' if self._prefer_pinned else ' [Windows default]'}")

    def _close_stream(self):
        if self.stream is None:
            return
        try:
            self.stream.stop()
            self.stream.close()
        except Exception as e:
            print(f"Error closing stream: {e}")
        finally:
            self.stream = None

    def prewarm(self):
        """Open the mic ahead of the first hotkey press so that take isn't slow either.

        Safe to call off the main thread; a failure here is not fatal because start()
        opens the stream on demand anyway.
        """
        try:
            self._ensure_stream()
        except Exception as e:
            print(f"Mic prewarm failed (will retry on first take): {e}")

    def start(self):
        self.frames.clear()
        self._peak = 0
        self._clipped = 0
        self._total_samples = 0
        # Drain any old data from the queue
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        # If the previous take came back silent, the mic endpoint is likely
        # wedged (an Intel SST / WASAPI app such as CallAssist left it stuck).
        # Re-arm it before capturing so this take isn't silent too. The re-arm
        # needs exclusive access, so the persistent stream must be closed first.
        if self._suspect_stuck:
            self._close_stream()
            reset_capture_endpoint()
            time.sleep(0.05)

        self._ensure_stream()
        self.is_recording = True  # set before start(): the callback drops idle audio
        self._start_time = time.time()
        try:
            self.stream.start()
        except Exception as e:
            # The device may have vanished (headphones unplugged) — rebuild once.
            print(f"Stream start failed ({e}); rebuilding.")
            self._close_stream()
            self._ensure_stream()
            self._start_time = time.time()
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
            try:
                self.stream.stop()  # stays OPEN — reopening costs ~700 ms (_ensure_stream)
            except Exception as e:
                print(f"Error stopping stream: {e}")
                self._close_stream()
        # A recording of real length that captured essentially no signal means
        # the mic endpoint is wedged (see reset_capture_endpoint). Flag it so the
        # NEXT take re-arms the endpoint first, self-healing without user action.
        self._suspect_stuck = duration > 0.5 and self._peak < SILENCE_PEAK_THRESHOLD
        if self._suspect_stuck:
            print(f"Capture looked stuck (peak={self._peak}); will re-arm mic on next take.")
            # The Windows default input gave us nothing (classic dead 3.5 mm jack mic).
            # Stop trusting it and pin to the built-in array from here on.
            if not self._prefer_pinned:
                self._prefer_pinned = True
                self._close_stream()
                print("Default input captured silence; falling back to the pinned mic.")
        # Zero frames means this stream delivered nothing at all (device pulled, or the
        # endpoint died under us). Drop it so the next take builds a fresh one instead
        # of reusing a stream that will stay silent forever.
        #
        # Only for takes long enough that frames SHOULD have arrived. A take of a few
        # milliseconds legitimately captures nothing (one block is 64 ms at 16 kHz), and
        # tearing the stream down for those made the next real dictation pay the ~700 ms
        # reopen again — reintroducing the very lost-audio bug this design removes.
        if duration > 0.5 and not self.frames:
            print(f"Take of {duration:.1f}s captured 0 frames; rebuilding the stream.")
            self._close_stream()
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

    @property
    def peak(self) -> int:
        """Loudest int16 sample of the last take (0 if nothing was captured)."""
        return self._peak

    @property
    def clip_percent(self) -> float:
        """Percentage of samples pinned at full scale. Above ~0.5% speech is audibly
        distorted; above ~2% transcription quality collapses."""
        if not self._total_samples:
            return 0.0
        return self._clipped / self._total_samples * 100.0

    def captured_sound(self) -> bool:
        """True if this take actually contains audio worth transcribing."""
        return bool(self.frames) and self._peak >= SILENCE_PEAK_THRESHOLD

    def get_duration(self) -> float:
        if not self.frames:
            return 0.0
        total_samples = sum(f.shape[0] for f in self.frames)
        return total_samples / SAMPLE_RATE
