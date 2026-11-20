"""
Supabase Storage for survey form files (photos, signatures).

The bucket `formulaires` is private (see migrations/20261025_formulaires.sql):
files are written with the service key and shown through short-lived signed
URLs. Talks to the Storage REST API directly, since the minimal
ServiceClient (core/config.py) has no storage client.
"""

import logging
from typing import Dict, Iterable, Optional

import httpx

from core import config
from core.services.errors import ServiceError

log = logging.getLogger(__name__)

BUCKET = "formulaires"
SIGNED_URL_SECONDS = 3600
_TIMEOUT = httpx.Timeout(30.0)


def _base() -> str:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_ROLE_KEY:
        raise ServiceError("L'envoi de fichiers est momentanément indisponible : la "
                           "configuration du serveur est incomplète.")
    return f"{config.SUPABASE_URL}/storage/v1"


def _headers() -> Dict[str, str]:
    key = config.SUPABASE_SERVICE_ROLE_KEY or ""
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def upload(path: str, content: bytes, content_type: str) -> None:
    """Store `content` at `path` in the bucket. Raises ServiceError."""
    try:
        response = httpx.post(f"{_base()}/object/{BUCKET}/{path}",
                              content=content, timeout=_TIMEOUT,
                              headers={**_headers(), "Content-Type": content_type,
                                       "x-upsert": "false"})
        response.raise_for_status()
    except ServiceError:
        raise
    except Exception as exc:
        log.exception("Storage upload of %s failed", path)
        raise ServiceError("Le fichier n'a pas pu être enregistré. Veuillez réessayer.") from exc


def download(path: str) -> Optional[bytes]:
    """The content of `path`, or None if it can't be read (best effort)."""
    try:
        response = httpx.get(f"{_base()}/object/{BUCKET}/{path}", headers=_headers(),
                             timeout=_TIMEOUT)
        response.raise_for_status()
        return response.content
    except Exception:
        log.warning("Could not download %s from storage", path)
        return None


def remove(paths: Iterable[str]) -> None:
    """Delete files (best effort: failures are only logged)."""
    paths = list(paths)
    if not paths:
        return
    try:
        httpx.request("DELETE", f"{_base()}/object/{BUCKET}", json={"prefixes": paths},
                      headers=_headers(), timeout=_TIMEOUT).raise_for_status()
    except Exception:
        log.exception("Could not remove orphan files %s", paths)


def signed_urls(paths: Iterable[str]) -> Dict[str, str]:
    """{path: temporary URL} for the given paths ({} if signing fails)."""
    paths = sorted({p for p in paths if p})
    if not paths:
        return {}
    try:
        response = httpx.post(f"{_base()}/object/sign/{BUCKET}",
                              json={"expiresIn": SIGNED_URL_SECONDS, "paths": paths},
                              headers=_headers(), timeout=_TIMEOUT)
        response.raise_for_status()
    except Exception:
        log.exception("Could not sign %d file URLs", len(paths))
        return {}
    urls = {}
    for item in response.json():
        signed = item.get("signedURL") or item.get("signedUrl")
        if signed and item.get("path"):
            urls[item["path"]] = f"{config.SUPABASE_URL}/storage/v1{signed}"
    return urls
