"""
Whoop OAuth + sync endpoints.

Flow (single user, personal use):
  1. GET /whoop/authorize  (Firebase Bearer token) -> { "authorize_url": ... }
     Open that URL in a browser and approve the Whoop consent screen.
  2. Whoop redirects to GET /whoop/callback?code=...&state=...
     The backend exchanges the code and persists tokens (rotating refresh
     token handled by WhoopManager). An initial data sync is kicked off.
  3. GET /whoop/status   -> connection + last-sync info
     POST /whoop/sync    -> manual sync trigger
"""

import logging

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse

from backend.api.auth import verify_token
from backend.managers.whoop_manager import WhoopManager
from backend.modules.whoop_module import WhoopModule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whoop")
whoop_manager = WhoopManager()


def _extract_bearer(authorization: str) -> str:
    if not authorization:
        raise HTTPException(status_code=400, detail="Authorization header is missing")
    parts = authorization.split(" ")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Authorization token is missing")
    return parts[1]


@router.get("/authorize")
async def authorize(authorization: str = Header(None)) -> dict:
    """Returns the Whoop consent URL for the authenticated user."""
    uid = await verify_token(_extract_bearer(authorization))
    return {"authorize_url": whoop_manager.build_authorize_url(uid)}


@router.get("/callback")
async def callback(code: str = None, state: str = None, error: str = None) -> HTMLResponse:
    """OAuth redirect target. Exchanges the code and stores tokens."""
    if error:
        logger.error(f"Whoop OAuth error: {error}")
        return HTMLResponse(f"<h3>Whoop connection failed: {error}</h3>", status_code=400)
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    uid = whoop_manager.resolve_oauth_state(state)
    if uid is None:
        raise HTTPException(status_code=400, detail="Unknown or expired OAuth state")

    await whoop_manager.exchange_authorization_code(uid, code)

    # Kick off an initial backfill so the coach has data immediately.
    try:
        counts = await WhoopModule.sync_user_data(uid)
    except Exception as e:
        logger.error(f"Initial Whoop sync failed for user {uid}: {e}")
        counts = {}

    return HTMLResponse(
        "<h3>Whoop connected ✔</h3>"
        f"<p>Initial sync: {counts or 'queued'}. You can close this window.</p>"
    )


@router.get("/status")
async def status(authorization: str = Header(None)) -> dict:
    uid = await verify_token(_extract_bearer(authorization))
    connected = await whoop_manager.is_connected(uid)
    last_synced_at = None
    if connected:
        meta_doc = await whoop_manager._token_doc_ref(uid).get()
        last_synced_at = (meta_doc.to_dict() or {}).get("last_synced_at")
    return {"connected": connected, "last_synced_at": last_synced_at}


@router.post("/sync")
async def sync(authorization: str = Header(None)) -> dict:
    uid = await verify_token(_extract_bearer(authorization))
    counts = await WhoopModule.sync_user_data(uid)
    return {"synced": counts}
