"""
Smart Saha - centralised configuration.

Loads the environment from the project's `.env` file and exposes:

  Config                - Flask settings, applied in app.py.
  get_anon_client()     - public Supabase client (sign up / sign in, and the
                          base for per-request member clients). Row Level
                          Security applies.
  get_service_client()  - administrator client built on
                          SUPABASE_SERVICE_ROLE_KEY. Bypasses Row Level
                          Security: server-side use only, never send its key
                          or its results unfiltered to the browser.

  get_user_client(tok)  - lightweight PostgREST client carrying a member's
                          access token (Row Level Security applies), used for
                          every authenticated request once the token is
                          verified (core/services/session_service.py).

get_anon_client() returns a *new* client on every call: a Supabase client
holds the session of whoever used it, so it must never be shared between
requests or members. The service client holds no session and is shared by
the whole process. Every PostgREST client reuses one pool of HTTPS
connections: with Supabase ~0.5 s away, a new TLS handshake per query
roughly doubled the cost of each call.
"""

import os
import threading
from datetime import timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv
from gotrue import SyncGoTrueClient
from postgrest import SyncPostgrestClient
from postgrest.constants import (
    DEFAULT_POSTGREST_CLIENT_HEADERS, DEFAULT_POSTGREST_CLIENT_TIMEOUT,
)
from postgrest.utils import SyncClient
from supabase import Client, ClientOptions, create_client

# Project root (the folder holding run.py, templates/ and .env).
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# flask/.env first, then the repository's .env (smartsahacoop/.env) for the
# keys it does not set. Flask's app.run() only loads the .env closest to the
# working directory, so a flask/.env (even an empty one) would hide the
# repository one. Variables already in the environment still win.
load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv(os.path.join(os.path.dirname(BASE_DIR), ".env"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
# Public keys of the project's JWT signing keys (verify access tokens
# locally instead of asking Supabase Auth on every request). Optional.
SUPABASE_JWKS_URL = os.environ.get("SUPABASE_JWKS_URL")

# Outgoing e-mail: the credentials sent to new staff members
# (core/services/mail_service.py). SMTP_SECURITY: starttls (default, port
# 587), ssl (port 465) or none. MAIL_BACKEND=console only writes the e-mails
# to the log, for development without a mail server.
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT") or 587)
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USER
SMTP_SECURITY = (os.environ.get("SMTP_SECURITY") or "starttls").lower()
MAIL_BACKEND = (os.environ.get("MAIL_BACKEND") or "smtp").lower()

# NASA FIRMS fire hotspots (core/services/fire_service.py). MAP_KEY stays on
# the server: clients only read the cache kept in Supabase. FIRMS publishes
# near-real-time data a few times a day, so the API is called at most once
# per FIRE_REFRESH_MINUTES, for the bounding box of the registered parcels.
MAP_KEY = os.environ.get("MAP_KEY")
FIRE_SOURCES = tuple(s.strip() for s in (os.environ.get("FIRE_SOURCES")
                     or "VIIRS_SNPP_NRT,VIIRS_NOAA20_NRT,VIIRS_NOAA21_NRT").split(",") if s.strip())
FIRE_REFRESH_MINUTES = max(15, int(os.environ.get("FIRE_REFRESH_MINUTES") or 180))
# Alerts are recorded within the largest radius; the maps filter 1 km / 5 km.
FIRE_ALERT_RADIUS_KM = float(os.environ.get("FIRE_ALERT_RADIUS_KM") or 5)
# Background refresh thread (FIRE_SCHEDULER=0: use `flask fires-refresh` from cron).
FIRE_SCHEDULER = os.environ.get("FIRE_SCHEDULER", "1") != "0"

# KoboToolbox (core/services/kobo_service.py). Synchronisations run on demand
# from the Formulaires menu; KOBO_SYNC_MINUTES > 0 also refreshes every
# imported form in the background (only new submissions are downloaded).
KOBO_SYNC_MINUTES = int(os.environ.get("KOBO_SYNC_MINUTES") or 0)

# Bootstrap account promoted to the `superadmin` role at startup
# (see core/middleware/superadmin.py). Both optional.
SUPER_ADMIN_MAIL = os.environ.get("SUPER_ADMIN_MAIL")
SUPER_ADMIN_PASSWORD = os.environ.get("SUPER_ADMIN_PASSWORD")


# How long a sign-in stays valid, for every role (Flask cookie + refresh):
# 3 months by default, unless the member signs out. SESSION_LIFETIME_DAYS
# in the environment changes it.
def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


SESSION_LIFETIME = timedelta(days=_positive_int("SESSION_LIFETIME_DAYS", 90))


class Config:
    """Flask settings. Loaded by app.py with app.config.from_object()."""

    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-only-not-secure")
    MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB upload limit
    # WTForms' built-in messages in French (core/forms/base.py).
    WTF_I18N_ENABLED = False

    # Every sign-in lasts SESSION_LIFETIME, whatever the role: the cookie is
    # permanent, and session_service.restore_session() ends the session once
    # that much time has passed since the sign-in (an absolute limit, not
    # extended by activity). Inside it, Supabase access tokens (1 h) are
    # refreshed automatically with the refresh token kept in the cookie.
    PERMANENT_SESSION_LIFETIME = SESSION_LIFETIME
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # HTTPS only in production (SESSION_COOKIE_SECURE=1); off by default so
    # the http:// dev server keeps working.
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"
    # Send the cookie only when the session changed (sign-in, token refresh).
    # Re-sending it on every response let a slow request write back tokens
    # it had read before a concurrent refresh: an already-used refresh token
    # that Supabase then rejected, logging the member out about an hour
    # after the sign-in. The cookie's expiry still moves forward at every
    # refresh (hourly while in use); the 3-month limit is enforced by
    # session_service.restore_session() from `signed_in_at`.
    SESSION_REFRESH_EACH_REQUEST = False

    # Windows resolves the default TEMP folder to an 8.3 short path (e.g.
    # "RAHERI~1"), and GDAL/fiona can mis-parse the "~" in that path when
    # writing KML/Shapefile, raising "WinError 267: The directory name is
    # invalid". Using our own temp folder in the project sidesteps that.
    EXPORT_TMP_DIR = os.path.join(BASE_DIR, "tmp_exports")

    # Create / promote the SUPER_ADMIN_MAIL account at startup
    # (SUPERADMIN_BOOTSTRAP=0 in the environment turns it off).
    SUPERADMIN_BOOTSTRAP = os.environ.get("SUPERADMIN_BOOTSTRAP", "1") != "0"


class ConfigurationError(RuntimeError):
    """A required environment variable is missing."""


def _require(name: str, value: Optional[str]) -> str:
    if not value:
        raise ConfigurationError(f"{name} n'est pas défini (vérifiez le fichier .env).")
    return value


def get_anon_client() -> Client:
    """A fresh client using the public anon key, for actions that don't
    require a logged-in user yet (signing up, signing in). Attach a member's
    tokens with `client.auth.set_session(...)` to query as that member.

    No automatic refresh, no persisted session: by default a supabase-py
    client starts a hidden timer whenever it holds a session and, about an
    hour later, refreshes the token on its own. That used up the refresh
    token stored in the member's cookie, so the next refresh by Flask was
    refused by Supabase as a reused token, which revokes the whole session:
    members were signed out about an hour after signing in. Flask alone
    refreshes tokens (session_service), and saves the new pair in the
    cookie."""
    return create_client(
        _require("SUPABASE_URL", SUPABASE_URL),
        _require("SUPABASE_KEY", SUPABASE_KEY),
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    )


class _SharedTransport(httpx.HTTPTransport):
    """The process-wide connection pool. Closing one client must not close
    the pool the other clients are using."""

    def close(self) -> None:
        pass

    def __exit__(self, *args) -> None:
        # httpx closes the pool directly on `with` exit, bypassing close().
        pass


_TRANSPORT = _SharedTransport(retries=1)


class _PooledPostgrestClient(SyncPostgrestClient):
    """SyncPostgrestClient whose HTTP session draws from the shared pool."""

    def create_session(self, base_url, headers, timeout, verify=True):
        return SyncClient(base_url=base_url, headers=headers, timeout=timeout,
                          transport=_TRANSPORT)


def _postgrest(api_key: str, bearer: str) -> SyncPostgrestClient:
    return _PooledPostgrestClient(
        f"{_require('SUPABASE_URL', SUPABASE_URL)}/rest/v1",
        headers={**DEFAULT_POSTGREST_CLIENT_HEADERS,
                 "apikey": api_key, "Authorization": f"Bearer {bearer}"},
        timeout=DEFAULT_POSTGREST_CLIENT_TIMEOUT,
    )


# One HTTP client on the shared pool for Supabase Auth calls made without a
# member session (sign-in). Its requests carry their own headers, so it is
# safe to share; it keeps the TLS connection to Supabase open between
# sign-ins instead of opening a new one (~0.2-0.3 s) for each.
_auth_http: Optional[httpx.Client] = None
_auth_http_lock = threading.Lock()


def get_auth_client() -> SyncGoTrueClient:
    """A Supabase Auth client for signing in, on the shared connection pool.
    Each call returns a new client (it holds the session it signs in), but
    they all reuse the same open HTTPS connections."""
    global _auth_http
    if _auth_http is None:
        with _auth_http_lock:
            if _auth_http is None:
                from gotrue.http_clients import SyncClient as GoTrueHttpClient
                _auth_http = GoTrueHttpClient(transport=_TRANSPORT, timeout=20)
    anon_key = _require("SUPABASE_KEY", SUPABASE_KEY)
    return SyncGoTrueClient(
        url=f"{_require('SUPABASE_URL', SUPABASE_URL)}/auth/v1",
        headers={"apikey": anon_key, "Authorization": f"Bearer {anon_key}"},
        http_client=_auth_http,
        auto_refresh_token=False,
        persist_session=False,
    )


def warm_up() -> None:
    """Open the HTTPS connections to Supabase before the first request:
    with Supabase ~0.5 s away, the first sign-in otherwise also paid the
    TLS handshakes. Best effort, run in a background thread at startup."""
    try:
        url = _require("SUPABASE_URL", SUPABASE_URL)
        key = _require("SUPABASE_KEY", SUPABASE_KEY)
        get_auth_client()                      # creates the shared HTTP client
        _auth_http.get(f"{url}/auth/v1/health", headers={"apikey": key})
        # The roles table, read on almost every request, cached per process.
        from core.models.repositories import RoleRepository
        service = get_service_client()
        if service is not None:
            RoleRepository(service).id_to_name()
    except Exception:
        pass


class ServiceClient:
    """Minimal administrator client: `.table(name)` for PostgREST queries and
    `.auth.admin` for the Supabase Auth admin API - the same surface as a
    supabase-py Client for those two uses.

    It is assembled from the underlying postgrest / gotrue clients because
    supabase-py 2.7.4's create_client() only accepts legacy JWT-shaped keys
    and rejects the newer `sb_secret_...` keys with "Invalid API key". Both
    key formats work here."""

    def __init__(self, url: str, service_key: str) -> None:
        auth_headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
        }
        self.postgrest = _postgrest(service_key, service_key)
        # No session to persist or refresh: every call is authenticated by
        # the service key itself.
        self.auth = SyncGoTrueClient(
            url=f"{url}/auth/v1",
            headers=auth_headers,
            auto_refresh_token=False,
            persist_session=False,
        )

    def table(self, table_name: str):
        """A PostgREST request builder on `table_name` (service key)."""
        return self.postgrest.table(table_name)

    def rpc(self, func: str, params: dict):
        """A PostgREST request calling the SQL function `func` (service key)."""
        return self.postgrest.rpc(func, params)


_service_client: Optional[ServiceClient] = None
_service_lock = threading.Lock()


def get_service_client() -> Optional[ServiceClient]:
    """The administrator client using SUPABASE_SERVICE_ROLE_KEY, or None if
    no service key is configured (callers then fall back to the anon client
    and Row Level Security). It holds no member session, so one instance
    serves the whole process."""
    global _service_client
    if not SUPABASE_SERVICE_ROLE_KEY:
        return None
    if _service_client is None:
        with _service_lock:
            if _service_client is None:
                _service_client = ServiceClient(_require("SUPABASE_URL", SUPABASE_URL),
                                                SUPABASE_SERVICE_ROLE_KEY)
    return _service_client


class UserClient:
    """A member's PostgREST client: `.table()` and `.rpc()` run with the
    member's access token, so Row Level Security applies. Without a token it
    queries as anon. Cheap to build: one per request."""

    def __init__(self, access_token: Optional[str]) -> None:
        anon_key = _require("SUPABASE_KEY", SUPABASE_KEY)
        self.postgrest = _postgrest(anon_key, access_token or anon_key)

    def table(self, table_name: str):
        """A PostgREST request builder on `table_name` (member's token)."""
        return self.postgrest.table(table_name)

    def rpc(self, func: str, params: dict):
        """A PostgREST request calling the SQL function `func` (member's token)."""
        return self.postgrest.rpc(func, params)


def get_user_client(access_token: Optional[str]) -> UserClient:
    """A PostgREST client acting as the member holding `access_token`."""
    return UserClient(access_token)
