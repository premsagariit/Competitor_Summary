"""
Cooperative cancellation for the long-running async calls inside Phase 1
(scraper.main) and Phase 2 (Gemini metric extraction) - the two stages slow
enough (network/LLM-bound, real filings can take many minutes) that a user
clicking "End Pipeline" mid-phase actually needs a fast response, not just a
between-phases check.

Deliberately not usable for the ThreadPoolExecutor-based PDF parsing stage
(prefetch_income_statements) - a running worker thread can't be interrupted
this way, only kept from picking up more work; that stage checks
`should_cancel` directly between completions instead of using this module.
"""
import asyncio


class PipelineCancelled(Exception):
    """Raised in place of a coroutine's own result once RunRegistry.cancel()
    has asked a running pipeline to stop."""


async def _run_cancellable(coro, should_cancel, poll_interval=0.4):
    task = asyncio.ensure_future(coro)
    while not task.done():
        if should_cancel():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise PipelineCancelled()
        await asyncio.sleep(poll_interval)
    return task.result()


def run_cancellable(coro, should_cancel):
    """Sync entrypoint mirroring `asyncio.run(coro)`, but polling
    `should_cancel()` every 0.4s while it runs - and, if it ever returns
    True, cancelling `coro` at its next await point (interrupting an
    in-flight network/LLM call almost immediately) and raising
    PipelineCancelled instead of returning `coro`'s result."""
    return asyncio.run(_run_cancellable(coro, should_cancel))
