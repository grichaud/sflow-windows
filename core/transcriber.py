import io
import os
from groq import Groq
from config import GROQ_MODEL, WHISPER_LANGUAGE, WHISPER_PROMPT


class Transcriber:
    def __init__(self):
        self._client = None

    def _get_client(self) -> Groq:
        """Lazy init: creates client on first use so API key from first-run dialog works."""
        if self._client is None:
            key = os.getenv("GROQ_API_KEY", "")
            if not key:
                raise ValueError("GROQ_API_KEY not configured")
            self._client = Groq(api_key=key, timeout=10.0)
        return self._client

    def transcribe(self, wav_buffer: io.BytesIO) -> str:
        """Send WAV audio to Groq Whisper and return transcribed text."""
        wav_buffer.seek(0)
        data = wav_buffer.read()
        if len(data) < 100:
            return ""
        kwargs = {}
        if WHISPER_PROMPT:  # empty = don't send the param at all (see config.py)
            kwargs["prompt"] = WHISPER_PROMPT
        transcription = self._get_client().audio.transcriptions.create(
            file=("recording.wav", data),
            model=GROQ_MODEL,
            language=WHISPER_LANGUAGE,
            response_format="text",
            temperature=0.0,
            **kwargs,
        )
        text = transcription.strip() if isinstance(transcription, str) else str(transcription).strip()
        return text
