"""Run independent Supabase queries at the same time.

Each query costs a round trip to Supabase (~0.3-0.5 s from here) and a page
often needs several that don't depend on each other: running them together
makes the page cost one round trip instead of the sum.

The callables run in worker threads, where Flask's `g` and `session` are not
available: resolve the client first (user_db() / service_db()) and capture
it in the callable. The PostgREST clients share one thread-safe httpx pool
(core/config.py).
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Tuple

# Enough for the widest page (4 queries) of several requests at once.
_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="supabase")


class Failed:
    """Placeholder for a call that raised: `error` is the exception."""

    def __init__(self, error: BaseException) -> None:
        self.error = error


def parallel(*calls: Callable[[], Any], return_errors: bool = False) -> Tuple[Any, ...]:
    """Run `calls` concurrently and return their results in order.

    By default the first exception (in call order) is re-raised once every
    call has finished. With return_errors=True a failing call gives a Failed
    instead, so the caller can degrade gracefully per query."""
    if len(calls) <= 1:
        calls_results = []
        for call in calls:
            try:
                calls_results.append(call())
            except Exception as exc:
                if not return_errors:
                    raise
                calls_results.append(Failed(exc))
        return tuple(calls_results)

    futures = [_POOL.submit(call) for call in calls]
    results = []
    for future in futures:
        try:
            results.append(future.result())
        except Exception as exc:
            results.append(Failed(exc))
    if not return_errors:
        for result in results:
            if isinstance(result, Failed):
                raise result.error
    return tuple(results)
