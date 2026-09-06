"""Process memory budget, so a heavy filing degrades instead of killing the box.

Render's free plan caps the container at 512MB and enforces it with the
Linux OOM killer - a SIGKILL the process cannot catch, log, or clean up
after. There is no MemoryError to handle either: with overcommit, the
kernel kills the container before CPython's allocator ever fails. The only
workable strategy is therefore to stay *below* the ceiling deliberately:
sample RSS at points where work can still be abandoned safely, and abort
that unit of work while the process is still healthy.

`MEMORY_BUDGET_MB` is a SOFT ceiling, and it is genuinely soft: the process
overshoots it before unwinding. A checkpoint only fires between pages, the
abandoned document is still alive while the exception propagates, and the
next filing may start before the collector catches up. Measured with a
deliberately tight 129MB budget, peak RSS reached 178MB - roughly 50MB of
overshoot, far more than the 1.5MB average / 15MB worst-case cost of a
single page would suggest. So set the budget at least ~100MB below the hard
limit; sizing it just under the cap would still get the container killed.

0 (the default) disables the check entirely, so development machines and
tests are unaffected and only deployments opt in.
"""
import os


class MemoryBudgetExceeded(RuntimeError):
    """Raised at a checkpoint when RSS has crossed MEMORY_BUDGET_MB.

    Callers are expected to abandon the current unit of work (one filing),
    let it be collected, and carry on with the rest of the run."""


def budget_mb() -> int:
    """Read per call, not cached at import, so tests and the API can change
    it without reloading the module."""
    try:
        return int(os.getenv("MEMORY_BUDGET_MB", "0"))
    except ValueError:
        return 0


def rss_mb():
    """Resident set size in MB, or None where it cannot be determined.

    None means "no opinion" and every caller treats it as within budget - a
    platform without a usable probe must not block the run."""
    try:
        with open("/proc/self/status", "r") as f:          # Linux (Render)
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:                                                    # Windows (dev)
        import ctypes
        from ctypes import wintypes as w

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.K32GetProcessMemoryInfo
        fn.argtypes = [w.HANDLE, ctypes.POINTER(_PMC), w.DWORD]
        fn.restype = w.BOOL
        c = _PMC()
        c.cb = ctypes.sizeof(_PMC)
        if fn(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return c.WorkingSetSize / 1024 / 1024
    except Exception:
        pass
    return None


def check(context: str) -> None:
    """Abort `context`'s work if RSS has crossed the budget.

    Call only where raising is safe - between pages, between companies -
    never mid-write, since the caller has to be able to drop what it was
    building and let it be collected."""
    limit = budget_mb()
    if limit <= 0:
        return
    current = rss_mb()
    if current is not None and current >= limit:
        raise MemoryBudgetExceeded(
            f"{context}: memory budget reached ({current:.0f}MB of "
            f"{limit}MB). Abandoning this document so the run can continue; "
            f"raise MEMORY_BUDGET_MB only if the container has the headroom.")
