"""
Background refresh of the fire hotspots (core/services/fire_service.py).

One daemon thread per process wakes up every few minutes and asks
fire_service.refresh() to run: Supabase (fire_claim_fetch) only lets it call
FIRMS when the cache is older than FIRE_REFRESH_MINUTES and no other worker
is fetching, so several processes never multiply the FIRMS calls.

FIRE_SCHEDULER=0 turns the thread off; then run `flask --app run
fires-refresh` from cron instead.
"""

import logging
import threading

from core import config

log = logging.getLogger(__name__)

_START_DELAY_S = 30
_started = False
_lock = threading.Lock()


def _check_every_s() -> int:
    # Often enough to honour the interval, rarely enough to cost nothing.
    return max(300, min(config.FIRE_REFRESH_MINUTES * 60 // 4, 1800))


def _loop(stop: threading.Event) -> None:
    from core.services import fire_service
    if stop.wait(_START_DELAY_S):
        return
    while True:
        try:
            result = fire_service.refresh()
            if result.skipped:
                log.debug("Fire refresh skipped: %s", result.skipped)
        except Exception:
            log.exception("Scheduled fire refresh failed")
        if stop.wait(_check_every_s()):
            return


def start() -> None:
    """Start the refresh thread once per process (no-op without MAP_KEY)."""
    global _started
    if not config.FIRE_SCHEDULER or not config.MAP_KEY or not config.SUPABASE_SERVICE_ROLE_KEY:
        if not config.MAP_KEY:
            log.warning("MAP_KEY is not set: fire hotspots will not be refreshed.")
        return
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, args=(threading.Event(),), name="fire-refresh",
                     daemon=True).start()
