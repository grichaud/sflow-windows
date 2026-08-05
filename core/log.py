"""Persistent logging.

SFlow runs under pythonw.exe: no console, and stdout/stderr are None (or a black
hole). Every traceback, Groq API error and dropped hotkey has been invisible, so
"it stopped working" could only be guessed at. Everything lands in LOG_PATH instead.
"""
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

from config import LOG_PATH

_logger: logging.Logger | None = None


class _StreamToLog:
    """File-like shim so stray print()/traceback output reaches the log.

    Under pythonw.exe sys.stdout/sys.stderr are None, which makes any library that
    writes to them raise AttributeError. This keeps that output instead of losing it.
    """

    def __init__(self, level: int):
        self._level = level
        self._buf = ""

    def write(self, data):
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                logging.getLogger("sflow.stream").log(self._level, line.rstrip())

    def flush(self):
        if self._buf.strip():
            logging.getLogger("sflow.stream").log(self._level, self._buf.rstrip())
        self._buf = ""

    def isatty(self):
        return False


def setup_logging() -> logging.Logger:
    """Configure file logging + global exception capture. Idempotent."""
    global _logger
    if _logger is not None:
        return _logger

    logger = logging.getLogger("sflow")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    # The pid is not decoration: two instances share this one file, and without it
    # their interleaved lines look like one process behaving impossibly (a hotkey
    # counter that resets, transcriptions with no matching keypress).
    handler.setFormatter(
        logging.Formatter("%(asctime)s pid=%(process)d %(levelname)-7s [%(name)s] %(message)s")
    )
    logging.getLogger().addHandler(handler)  # root, so library output is captured too
    logging.getLogger().setLevel(logging.INFO)

    # Opt-in key tracing: logs every Ctrl/Alt event with its injected flag, to identify
    # what breaks the shortcut mid-dictation. Off by default — it is noisy and adds work
    # inside the keyboard hook. Enable with SFLOW_TRACE_KEYS=1.
    if os.getenv("SFLOW_TRACE_KEYS"):
        logging.getLogger("sflow.hotkey").setLevel(logging.DEBUG)
        logger.info("key tracing ENABLED (SFLOW_TRACE_KEYS)")

    sys.stdout = _StreamToLog(logging.INFO)
    sys.stderr = _StreamToLog(logging.ERROR)

    def _excepthook(exc_type, exc, tb):
        logger.critical("UNCAUGHT EXCEPTION", exc_info=(exc_type, exc, tb))

    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        # A crash in the pynput listener thread kills the hotkey with no other trace.
        logger.critical(
            "UNCAUGHT EXCEPTION in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_excepthook

    _logger = logger
    return logger


def get_logger(name: str = "sflow") -> logging.Logger:
    """Get a logger. Deliberately does NOT call setup_logging(): merely importing a
    module must never redirect the process's stdout/stderr. main.py sets that up once;
    until it does, these loggers are harmless no-ops (which is what tests want)."""
    return logging.getLogger(name)
