"""
Flask session <-> Supabase session.

The Flask session (signed cookie) holds the Supabase access / refresh
tokens, the user id and - for display only - the role name. On each
authenticated request restore_session() verifies the access token and gives
the request a client carrying it (g.db).

Verification avoids a round trip to Supabase Auth (~0.5 s) whenever it can:
the token's signature is checked locally against the project's public keys
(SUPABASE_JWKS_URL, needs the `cryptography` package), or else a token
Supabase already accepted is remembered until it expires. Only an expired
(or unknown) token goes to Supabase, which refreshes it; refreshed tokens are
saved back into the cookie: Supabase refresh tokens are single-use, so
keeping the old pair would log the member out on the next refresh.

Concurrent requests (page polling, prefetch, notifications) often find the
same expired token at once. Refreshes of one refresh token are therefore
serialised, and every rotation old -> new is remembered for a while: a
request still carrying the old pair (or a cookie written back by a slow
response) is moved to the newest pair instead of presenting a used refresh
token to Supabase, which would reject it and revoke the whole session.
This coordination is per process: run Flask as one multi-threaded process
(or route each member to the same worker); Supabase's own refresh token
reuse interval (10 s by default) covers the rest.
"""

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from enum import Enum
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import jwt
from flask import g, session
from gotrue.errors import AuthError, AuthRetryableError

from core import config

log = logging.getLogger(__name__)


class SessionStatus(str, Enum):
    """Outcome of restore_session()."""
    OK = "ok"                    # tokens verified, g.auth_user set
    MISSING = "missing"          # no tokens in the cookie
    INVALID = "invalid"          # Supabase rejected the tokens
    UNAVAILABLE = "unavailable"  # Supabase unreachable - tokens kept


def has_session() -> bool:
    """Cheap check: does the cookie hold tokens at all?"""
    return bool(session.get("access_token") and session.get("refresh_token"))


# --------------------------------------------------------------------------
# Access token verification
# --------------------------------------------------------------------------
# Tokens this close to expiry are refreshed by Supabase instead.
_EXPIRY_MARGIN_S = 30
_CACHE_SIZE = 5000


class _TokenVerifier:
    """Verified access tokens -> user, without asking Supabase Auth again."""

    def __init__(self) -> None:
        self._cache: "OrderedDict[str, Tuple[Any, int]]" = OrderedDict()
        self._lock = threading.Lock()
        self._jwks: Optional[jwt.PyJWKClient] = None
        if config.SUPABASE_JWKS_URL:
            try:
                import cryptography  # noqa: F401  (ES256 / RS256 support)
                self._jwks = jwt.PyJWKClient(config.SUPABASE_JWKS_URL,
                                             cache_keys=True, lifespan=3600,
                                             timeout=5)
            except ImportError:
                log.warning("Install `cryptography` to verify Supabase tokens "
                            "locally; falling back to cached Auth lookups.")

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def expiry(token: str) -> int:
        """The token's `exp` claim (0 if unreadable). Not a verification."""
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
            return int(claims.get("exp") or 0)
        except jwt.PyJWTError:
            return 0

    def verify(self, token: str) -> Optional[Any]:
        """The user of a valid, not-about-to-expire token, or None when
        Supabase must be asked (expired, unknown, or keys unreachable)."""
        exp = self.expiry(token)
        if exp - time.time() < _EXPIRY_MARGIN_S:
            return None
        key = self._key(token)
        with self._lock:
            hit = self._cache.get(key)
        if hit:
            return hit[0]
        if self._jwks is None:
            return None
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(token, signing_key.key,
                                algorithms=["ES256", "RS256"],
                                audience="authenticated")
        except Exception as exc:
            log.info("Local token verification unavailable: %s", exc)
            return None
        user = SimpleNamespace(id=claims["sub"], email=claims.get("email"),
                               user_metadata=claims.get("user_metadata") or {})
        self.remember(token, user, exp)
        return user

    def remember(self, token: str, user: Any, exp: Optional[int] = None) -> None:
        """Trust `token` for `user` until it expires."""
        exp = exp or self.expiry(token)
        if exp <= time.time():
            return
        with self._lock:
            self._cache[self._key(token)] = (user, exp)
            while len(self._cache) > _CACHE_SIZE:
                self._cache.popitem(last=False)

    def forget(self, token: Optional[str]) -> None:
        """Stop trusting `token` (sign-out)."""
        if token:
            with self._lock:
                self._cache.pop(self._key(token), None)


tokens = _TokenVerifier()


# --------------------------------------------------------------------------
# Refresh coordination (see the module docstring)
# --------------------------------------------------------------------------
# How long a rotation old refresh token -> new pair is remembered.
_ROTATION_TTL_S = 24 * 3600
_ROTATION_MAX = 20000
_ROTATION_CHAIN_MAX = 50
_LOCK_STRIPES = 64


class _Refresher:
    """Serialises the refreshes of a refresh token and remembers rotations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stripes: List[threading.Lock] = [threading.Lock() for _ in range(_LOCK_STRIPES)]
        # sha256(old refresh token) -> (new access, new refresh, user, when)
        self._rotated: "OrderedDict[str, Tuple[str, str, Any, float]]" = OrderedDict()

    @staticmethod
    def _key(refresh_token: str) -> str:
        return hashlib.sha256(refresh_token.encode()).hexdigest()

    def lock_for(self, refresh_token: str) -> threading.Lock:
        """The lock every refresh of `refresh_token` goes through."""
        return self._stripes[int(self._key(refresh_token)[:8], 16) % _LOCK_STRIPES]

    def latest(self, refresh_token: str) -> Optional[Tuple[str, str, Any]]:
        """The newest (access, refresh, user) `refresh_token` was rotated
        into, following successive rotations; None if it never was."""
        now = time.time()
        found = None
        key = self._key(refresh_token)
        with self._lock:
            for _ in range(_ROTATION_CHAIN_MAX):
                hit = self._rotated.get(key)
                if not hit:
                    break
                if now - hit[3] > _ROTATION_TTL_S:
                    self._rotated.pop(key, None)
                    break
                found = hit[:3]
                key = self._key(hit[1])
        return found

    def record(self, old_refresh: str, access_token: str, refresh_token: str, user: Any) -> None:
        """Remember that `old_refresh` was rotated into the new pair."""
        with self._lock:
            self._rotated[self._key(old_refresh)] = (access_token, refresh_token, user, time.time())
            self._rotated.move_to_end(self._key(old_refresh))
            while len(self._rotated) > _ROTATION_MAX:
                self._rotated.popitem(last=False)


refresher = _Refresher()


def _store_tokens(access_token: str, refresh_token: str) -> None:
    """Save the pair in the cookie, only if it changed (an unchanged session
    is not sent back: see Config.SESSION_REFRESH_EACH_REQUEST)."""
    if session.get("access_token") != access_token:
        session["access_token"] = access_token
    if session.get("refresh_token") != refresh_token:
        session["refresh_token"] = refresh_token


def restore_session() -> SessionStatus:
    """Verify the cookie's tokens for this request.

    Always sets g.db (a client carrying the member's token when valid, anon
    otherwise), g.auth_user (the verified user, or None) and g.auth_status."""
    g.db, g.auth_user, g.auth_status = (config.get_user_client(None), None,
                                        SessionStatus.MISSING)
    if not has_session():
        return g.auth_status

    access_token = session["access_token"]
    refresh_token = session["refresh_token"]

    # SESSION_LIFETIME (3 months) after the sign-in the session ends, even
    # if still in use. Sessions opened before this rule start it now.
    if not session.get("signed_in_at"):
        session["signed_in_at"] = int(time.time())
        session.permanent = True
    if time.time() - session["signed_in_at"] > config.SESSION_LIFETIME.total_seconds():
        end_session()
        g.auth_status = SessionStatus.INVALID
        return g.auth_status

    # The cookie carries a pair that was already rotated (concurrent
    # request, or written back by a slow response): use the newest one.
    newest = refresher.latest(refresh_token)
    if newest:
        access_token, refresh_token, _ = newest
        _store_tokens(access_token, refresh_token)

    user = tokens.verify(access_token)
    if user is None:
        status, user, access_token = _verify_with_supabase(access_token,
                                                           refresh_token)
        if status is not SessionStatus.OK:
            g.auth_status = status
            return status

    g.db = config.get_user_client(access_token)
    g.auth_user = user
    g.auth_status = SessionStatus.OK
    return g.auth_status


def _verify_with_supabase(access_token: str, refresh_token: str
                          ) -> Tuple[SessionStatus, Any, str]:
    """Ask Supabase Auth (refreshing an expired token). Returns the status,
    the user and the access token to use from now on. One refresh at a
    time per refresh token: the requests that waited reuse its result."""
    with refresher.lock_for(refresh_token):
        newest = refresher.latest(refresh_token)
        if newest:
            new_access, new_refresh, new_user = newest
            _store_tokens(new_access, new_refresh)
            user = tokens.verify(new_access) or new_user
            if tokens.expiry(new_access) - time.time() > _EXPIRY_MARGIN_S:
                return SessionStatus.OK, user, new_access
            access_token, refresh_token = new_access, new_refresh
        return _ask_supabase(access_token, refresh_token)


def _ask_supabase(access_token: str, refresh_token: str
                  ) -> Tuple[SessionStatus, Any, str]:
    client = config.get_anon_client()
    try:
        response = client.auth.set_session(access_token, refresh_token)
    except AuthRetryableError as exc:
        log.warning("Supabase auth unreachable: %s", exc)
        return SessionStatus.UNAVAILABLE, None, access_token
    except AuthError as exc:
        log.info("Supabase session rejected: %s", exc)
        return SessionStatus.INVALID, None, access_token
    except Exception:
        log.exception("Unexpected error while restoring the Supabase session")
        return SessionStatus.UNAVAILABLE, None, access_token

    new_session = response.session if response else None
    if not new_session or not new_session.user:
        return SessionStatus.INVALID, None, access_token

    if new_session.refresh_token != refresh_token:
        refresher.record(refresh_token, new_session.access_token,
                         new_session.refresh_token, new_session.user)
    _store_tokens(new_session.access_token, new_session.refresh_token)
    tokens.remember(new_session.access_token, new_session.user)
    return SessionStatus.OK, new_session.user, new_session.access_token


def adopt_user(access_token: str, user: Any) -> None:
    """Use the user Supabase just returned at sign-in for this request, and
    trust its token from now on (no second round trip)."""
    tokens.remember(access_token, user)
    g.db = config.get_user_client(access_token)
    g.auth_user = user
    g.auth_status = SessionStatus.OK


def open_session(access_token: str, refresh_token: str, user_id: str,
                 role: Optional[str], remember: bool = False) -> None:
    """Start a fresh Flask session for a member who just authenticated. It
    lasts config.SESSION_LIFETIME (3 months) for every role; `remember` is
    kept for older callers and no longer changes anything."""
    session.clear()  # never carry data over from a previous session
    session.permanent = True
    session["signed_in_at"] = int(time.time())
    session["access_token"] = access_token
    session["refresh_token"] = refresh_token
    session["user_id"] = user_id
    session["role"] = role


def set_display_role(role: Optional[str]) -> None:
    """Keep the cookie's display copy of the role in sync with the DB
    (written only when it changed, so the cookie is not re-sent)."""
    if session.get("role") != role:
        session["role"] = role


def end_session() -> None:
    """Forget the member: clear the whole Flask session."""
    tokens.forget(session.get("access_token"))
    session.clear()


def current_user() -> Any:
    """The verified Supabase user of this request (None if not verified)."""
    return g.get("auth_user")
