"""The factory's own logs, kept on disk so they outlive the console.

Each process writes its own rotating file in the log folder (SF_LOG_DIR, by default
factory-logs/ in the workspace): worker.log, web.log and cli.log. Separate files, because
on Windows a file another process holds open can't be rotated. Every line says which issue
it's about, when there is one, so `grep BEI-12 worker.log` gives one issue's story.

Agent runs also get a transcript each, in the issue's workspace (see Transcript).

Values of the factory's own secrets are masked in every line, in case one reaches an
exception message.
"""

import contextlib
import dataclasses
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from software_factory import config, store

LOG_DIR_NAME = "factory-logs"
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 10
TRANSCRIPTS_DIR = "runs"
# Secrets shorter than this would mask ordinary words
MIN_SECRET_LENGTH = 8
# Requests the web app's pages poll or load with every page; the file skips them
QUIET_PATHS = ("/api/attention", "/static/")

# The issue (and job) the current task works on; anyio tasks each get their own copy
_issue: ContextVar[str] = ContextVar("issue", default="-")


def log_dir() -> Path:
    # Read when set up, not at import, so a test's workspace is used
    return Path(os.getenv("SF_LOG_DIR") or store.WORKSPACE_DIR / LOG_DIR_NAME)


@contextlib.contextmanager
def issue_context(identifier: str, job: str | None = None):
    """Tag every line logged inside the block with the issue, e.g. [BEI-12 plan]."""
    token = _issue.set(f"{identifier} {job}" if job else identifier)
    try:
        yield
    finally:
        _issue.reset(token)


def current_issue() -> str:
    return _issue.get()


class _IssueFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "issue"):
            record.issue = _issue.get()
        return True


class _QuietRequests(logging.Filter):
    """Leaves out the web app's successful polls and static files, which would drown the rest."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "werkzeug" or record.levelno > logging.INFO:
            return True
        message = record.getMessage()
        return not any(path in message for path in QUIET_PATHS) or '" 5' in message


class _Formatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self):
        super().__init__("%(asctime)s %(levelname)-7s %(name)s [%(issue)s] %(message)s")

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def format(self, record: logging.LogRecord) -> str:
        return mask_secrets(super().format(record))


def _secret_values() -> list[str]:
    values = (os.getenv(name) or "" for name in config.FACTORY_SECRETS)
    return sorted({v for v in values if len(v) >= MIN_SECRET_LENGTH}, key=len, reverse=True)


def mask_secrets(text: str) -> str:
    for value in _secret_values():
        text = text.replace(value, "<secret>")
    return text


def describe(e: BaseException) -> str:
    """The exception, with a failed command's output, which its repr leaves out."""
    text = repr(e)
    if isinstance(e, subprocess.CalledProcessError):
        output = e.stderr or e.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        if output.strip():
            text += f"\n{output.strip()[-2000:]}"
    return text


_configured: str | None = None


def setup(process: str, level: str | None = None, console: bool = True) -> Path:
    """Send this process's logs to <log dir>/<process>.log, and to the console unless told not to.

    Safe to call twice; the second call does nothing. Returns the log file's path.
    """
    global _configured
    path = log_dir() / f"{process}.log"
    if _configured:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    formatter = _Formatter()
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True
    )
    file_handler.addFilter(_QuietRequests())
    handlers = [file_handler] + ([logging.StreamHandler(sys.stdout)] if console else [])
    root = logging.getLogger()
    root.setLevel((level or os.getenv("SF_LOG_LEVEL") or "INFO").upper())
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_IssueFilter())
        root.addHandler(handler)
    # One line per HTTP request is noise next to what the factory does with the answer
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _log_uncaught()
    _configured = process
    logging.getLogger(__name__).info("%s logging to %s", process, path)
    return path


def _log_uncaught():
    previous = sys.excepthook

    def hook(kind, value, tb):
        if not issubclass(kind, KeyboardInterrupt):
            logging.getLogger("software_factory").critical(
                "Uncaught exception, the process stops", exc_info=(kind, value, tb)
            )
        previous(kind, value, tb)

    sys.excepthook = hook


def transcript_path(issue_id: str, run_id: int, phase: str) -> Path:
    return store.issue_dir(issue_id) / TRANSCRIPTS_DIR / f"{run_id}-{phase}.jsonl"


def _jsonable(message) -> dict:
    if dataclasses.is_dataclass(message) and not isinstance(message, type):
        return dataclasses.asdict(message)
    return {"repr": repr(message)}


class Transcript:
    """Everything one agent run did, one JSON object per line, written as it happens.

    The first line describes the run (phase, model, prompt), then every message the SDK
    returns (what the agent said, each tool call and its result), the CLI's stderr, and,
    if the run crashed, the traceback. Each line is flushed, so a crash or a killed worker
    still leaves what happened up to that point.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._file = None
        # A transcript that can't be written mustn't stop the run it records
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("a", encoding="utf-8")
        except OSError:
            logging.getLogger(__name__).exception("Can't write the transcript %s; the run goes on without it", path)

    def write(self, kind: str, **data):
        try:
            line = json.dumps(
                {"at": datetime.now(UTC).isoformat(timespec="milliseconds"), "type": kind, **data},
                default=str,
                ensure_ascii=False,
            )
            with self._lock:
                if self._file and not self._file.closed:
                    self._file.write(mask_secrets(line) + "\n")
                    self._file.flush()
        except (OSError, TypeError, ValueError):
            logging.getLogger(__name__).exception("Couldn't add a %s line to %s", kind, self.path)

    def message(self, message):
        self.write(type(message).__name__, **_jsonable(message))

    def stderr(self, line: str):
        self.write("stderr", line=line.rstrip("\n"))

    def error(self, e: BaseException):
        self.write("error", error=describe(e), traceback="".join(traceback.format_exception(e)))

    def close(self):
        with self._lock:
            if self._file:
                self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
