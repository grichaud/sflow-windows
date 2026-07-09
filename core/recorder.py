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


class AudioRecorder:
    def __init__(self):
        self.audio_queue = queue.Queue()  # For UI visualization
        self.frames: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self.is_recording = False
        self._start_time = 0.0

    def _callback(self, indata: np.ndarray, frames: int, time_info, status):
        if status:
            print(f"Audio status: {status}")
        self.audio_queue.put(indata.copy())
        self.frames.append(indata.copy())

    def start(self):
        self.frames.clear()
        # Drain any old data from the queue
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        self.is_recording = True
        self._start_time = time.time()
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
