"""One SQL function per page (migrations/20261116_performance.sql).

Each page-level RPC replaces the queries a page used to run (in parallel,
core/services/concurrency.py) by a single round trip. Until the migration
has been run in Supabase, or if a call fails, call() returns None and the
service uses those queries instead: the page always works.
"""

import logging
from typing import Any, Dict, Optional, Set

log = logging.getLogger(__name__)

# Functions PostgREST reported as missing (PGRST202): not tried again.
_missing: Set[str] = set()


def call(db: Any, name: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """The result of the page RPC `name`, or None when the caller must fall
    back to its own queries (function missing, or the call failed)."""
    if name in _missing:
        return None
    try:
        return db.rpc(name, params or {}).execute().data
    except Exception as exc:
        text = str(exc)
        if "PGRST202" in text or "Could not find the function" in text:
            _missing.add(name)
            log.warning("%s() is missing: run migrations/20261116_performance.sql in "
                        "Supabase. Falling back to separate queries.", name)
        else:
            log.warning("%s() failed (%s): falling back to separate queries.", name, exc)
        return None
