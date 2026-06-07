"""
WhoopManager: OAuth 2.0 client + authenticated HTTP access to the Whoop v2 API.

Design notes
------------
- Tokens are stored per-user in Firestore at:
      studies/{STUDY_ID}/users/{uid}/integrations/whoop
  mirroring how other per-user state lives under the user doc.

- CRITICAL: Whoop uses *rotating* refresh tokens. Every token exchange
  (authorization code OR refresh) returns a NEW refresh token that invalidates
  the previous one. We therefore persist tokens to Firestore immediately after
  every exchange, and serialize refreshes with a per-user asyncio.Lock so two
  concurrent requests can't race and burn the same refresh token twice.

- Recovery is only available after a sleep cycle completes; callers should not
  assume the latest cycle has a recovery score (score_state may be
  PENDING_SCORE / UNSCORABLE, or the recovery may 404).
"""

import asyncio
import logging
import secrets
import time
from typing import Any, AsyncGenerator, Dict, Optional

import aiohttp

from backend import config
from backend.managers.firebase_manager import FirebaseManager

logger = logging.getLogger(__name__)

AUTH_URL = f"{config.WHOOP_API_BASE_URL}/oauth/oauth2/auth"
TOKEN_URL = f"{config.WHOOP_API_BASE_URL}/oauth/oauth2/token"
API_BASE = f"{config.WHOOP_API_BASE_URL}/developer/v2"

# Refresh access tokens this many seconds before they actually expire.
TOKEN_EXPIRY_MARGIN_SEC = 120


class WhoopNotConnectedError(Exception):
    """Raised when a user has no stored Whoop tokens (OAuth never completed or lost)."""


class WhoopManager:
    _instance = None

    def __new__(cls) -> "WhoopManager":
        if cls._instance is None:
            cls._instance = super(WhoopManager, cls).__new__(cls)
            cls._instance.firebase_manager = FirebaseManager()
            cls._instance._refresh_locks = {}  # uid -> asyncio.Lock
            cls._instance._pending_oauth_states = {}  # state -> uid
        return cls._instance

    # ------------------------------------------------------------------
    # OAuth flow
    # ------------------------------------------------------------------
    def build_authorize_url(self, uid: str) -> str:
        """Returns the Whoop consent URL for this user. State maps back to uid."""
        state = secrets.token_urlsafe(24)
        self._pending_oauth_states[state] = uid
        from urllib.parse import urlencode
        params = urlencode({
            "response_type": "code",
            "client_id": config.WHOOP_CLIENT_ID,
            "redirect_uri": config.WHOOP_REDIRECT_URI,
            "scope": config.WHOOP_OAUTH_SCOPES,
            "state": state,
        })
        return f"{AUTH_URL}?{params}"

    def resolve_oauth_state(self, state: str) -> Optional[str]:
        """Returns the uid for a pending OAuth state (one-shot), or None."""
        return self._pending_oauth_states.pop(state, None)

    async def exchange_authorization_code(self, uid: str, code: str) -> Dict[str, Any]:
        """Exchanges an authorization code for tokens and persists them."""
        token_data = await self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config.WHOOP_CLIENT_ID,
            "client_secret": config.WHOOP_CLIENT_SECRET,
            "redirect_uri": config.WHOOP_REDIRECT_URI,
        })
        await self._persist_tokens(uid, token_data)
        logger.info(f"Whoop connected for user {uid}")
        return token_data

    async def _refresh_tokens(self, uid: str, refresh_token: str) -> Dict[str, Any]:
        token_data = await self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.WHOOP_CLIENT_ID,
            "client_secret": config.WHOOP_CLIENT_SECRET,
            "scope": "offline",
        })
        # Persist IMMEDIATELY — the old refresh token is now dead.
        await self._persist_tokens(uid, token_data)
        return token_data

    async def _token_request(self, payload: Dict[str, str]) -> Dict[str, Any]:
        async with aiohttp.ClientSession() as session:
            async with session.post(TOKEN_URL, data=payload) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    logger.error(f"Whoop token request failed ({resp.status}): {body}")
                    raise RuntimeError(f"Whoop token request failed with status {resp.status}")
                return body

    # ------------------------------------------------------------------
    # Token persistence (Firestore)
    # ------------------------------------------------------------------
    def _token_doc_ref(self, uid: str):
        return self.firebase_manager.get_user_doc_ref(uid).collection("integrations").document("whoop")

    async def _persist_tokens(self, uid: str, token_data: Dict[str, Any]) -> None:
        expires_at = time.time() + float(token_data.get("expires_in", 3600))
        await self._token_doc_ref(uid).set({
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": expires_at,
            "scope": token_data.get("scope"),
            "token_type": token_data.get("token_type", "bearer"),
        }, merge=True)

    async def _load_tokens(self, uid: str) -> Dict[str, Any]:
        doc = await self._token_doc_ref(uid).get()
        if not doc.exists:
            raise WhoopNotConnectedError(f"No Whoop tokens stored for user {uid}")
        data = doc.to_dict() or {}
        if not data.get("refresh_token"):
            raise WhoopNotConnectedError(f"Whoop refresh token missing for user {uid}")
        return data

    async def is_connected(self, uid: str) -> bool:
        try:
            await self._load_tokens(uid)
            return True
        except WhoopNotConnectedError:
            return False

    async def get_valid_access_token(self, uid: str) -> str:
        """Returns a valid access token, refreshing (and re-persisting) if needed."""
        lock = self._refresh_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            tokens = await self._load_tokens(uid)
            if time.time() < float(tokens.get("expires_at", 0)) - TOKEN_EXPIRY_MARGIN_SEC:
                return tokens["access_token"]
            logger.info(f"Refreshing Whoop access token for user {uid}")
            new_tokens = await self._refresh_tokens(uid, tokens["refresh_token"])
            return new_tokens["access_token"]

    # ------------------------------------------------------------------
    # API access
    # ------------------------------------------------------------------
    async def get(self, uid: str, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Authenticated GET against the Whoop v2 API. Returns None on 404."""
        token = await self.get_valid_access_token(uid)
        url = f"{API_BASE}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params or {}, headers={"Authorization": f"Bearer {token}"}) as resp:
                if resp.status == 404:
                    return None
                if resp.status == 401:
                    # Token may have been revoked server-side; force one refresh and retry.
                    logger.warning(f"Whoop 401 for user {uid}, forcing token refresh and retrying once")
                    tokens = await self._load_tokens(uid)
                    await self._refresh_tokens(uid, tokens["refresh_token"])
                    return await self.get(uid, path, params)
                if resp.status == 429:
                    retry_after = float(resp.headers.get("Retry-After", 5))
                    logger.warning(f"Whoop rate limited, retrying in {retry_after}s")
                    await asyncio.sleep(retry_after)
                    return await self.get(uid, path, params)
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    logger.error(f"Whoop GET {path} failed ({resp.status}): {body}")
                    raise RuntimeError(f"Whoop GET {path} failed with status {resp.status}")
                return body

    async def paginate(self, uid: str, path: str, params: Optional[Dict[str, Any]] = None,
                       max_records: int = 500) -> AsyncGenerator[Dict[str, Any], None]:
        """Iterates records of a paginated collection endpoint (sorted by start desc)."""
        params = dict(params or {})
        count = 0
        while True:
            page = await self.get(uid, path, params)
            if page is None:
                return
            for record in page.get("records", []):
                yield record
                count += 1
                if count >= max_records:
                    return
            next_token = page.get("next_token")
            if not next_token:
                return
            params["nextToken"] = next_token

    # Convenience wrappers -------------------------------------------------
    async def get_cycles(self, uid: str, start: Optional[str] = None, end: Optional[str] = None,
                         limit: int = 25, max_records: int = 500) -> list:
        params: Dict[str, Any] = {"limit": min(limit, 25)}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        return [r async for r in self.paginate(uid, "/cycle", params, max_records=max_records)]

    async def get_recovery_for_cycle(self, uid: str, cycle_id: Any) -> Optional[Dict[str, Any]]:
        return await self.get(uid, f"/cycle/{cycle_id}/recovery")

    async def get_sleeps(self, uid: str, start: Optional[str] = None, end: Optional[str] = None,
                         max_records: int = 500) -> list:
        params: Dict[str, Any] = {"limit": 25}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        return [r async for r in self.paginate(uid, "/activity/sleep", params, max_records=max_records)]

    async def get_workouts(self, uid: str, start: Optional[str] = None, end: Optional[str] = None,
                           max_records: int = 500) -> list:
        params: Dict[str, Any] = {"limit": 25}
        if start:
            params["start"] = start
        if end:
            params["end"] = end
        return [r async for r in self.paginate(uid, "/activity/workout", params, max_records=max_records)]

    async def get_profile(self, uid: str) -> Optional[Dict[str, Any]]:
        return await self.get(uid, "/user/profile/basic")

    async def get_body_measurement(self, uid: str) -> Optional[Dict[str, Any]]:
        return await self.get(uid, "/user/measurement/body")
