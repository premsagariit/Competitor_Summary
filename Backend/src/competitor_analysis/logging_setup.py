"""Structured console logging shared by the CLI, the API server and the
pipeline itself: `[timestamp] [phase] [LEVEL] logger: message`.

Phase is carried via a contextvar rather than threaded through every function
signature - `with phase("Phase 2 / Gemini"):` at a stage boundary tags every
log record emitted anywhere in that call stack, including inside modules that
have no idea what phase they're running under.

Caveat: contextvars propagate automatically down a synchronous call stack and
into asyncio tasks/`asyncio.to_thread` (both copy the caller's Context), but
NOT into a plain `concurrent.futures.ThreadPoolExecutor` submission - each
pool thread starts with a fresh Context. Use `submit_with_phase` there.
"""
import contextvars
import logging

PHASE: contextvars.ContextVar[str] = contextvars.ContextVar("phase", default="startup")


class phase:
    """Context manager: tags every log record emitted inside the block with
    `name`, restoring the previous phase (if any) on exit - so nested
    `with phase(...)` blocks compose instead of clobbering each other."""

    def __init__(self, name: str):
        self._name = name

    def __enter__(self):
        self._token = PHASE.set(self._name)
        return self

    def __exit__(self, *exc_info):
        PHASE.reset(self._token)
        return False


class PhaseFilter(logging.Filter):
    """Attach to any handler that wants `record.phase` populated - the
    console handler `configure()` sets up, and `api/runs.py`'s activity-feed
    handler, which needs it independently since a Handler's filters are its
    own (adding one to the console handler doesn't affect another)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.phase = PHASE.get()
        return True


FORMAT = "[%(asctime)s] [%(phase)s] [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def configure(level: int = logging.INFO) -> logging.Logger:
    """Idempotent: safe to call from cli.py's main(), the API server's
    startup, and any test that imports both. Console-only by design - no
    per-run log files; the CLI's stdout is already what the Streamlit control
    panel and `api/runs.py`'s activity feed both capture."""
    global _configured
    root = logging.getLogger("competitor_analysis")
    root.setLevel(level)
    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
        handler.addFilter(PhaseFilter())
        root.addHandler(handler)
        root.propagate = False
        _configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def submit_with_phase(executor, fn, *args, **kwargs):
    """Use in place of `executor.submit(fn, *args, **kwargs)` for a plain
    ThreadPoolExecutor so log records emitted inside `fn` keep the phase
    active where the pool was created, instead of reporting "startup"."""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)
