# Whoop Integration — Setup & Architecture

Workstream 1 (Whoop + HealthKit data integration) is implemented. HealthKit stays
device-local and untouched; Whoop is integrated at the Python backend. Both fan
into Firestore and the coach queries both via `tool_module`.

## What was added

| File | Purpose |
|---|---|
| `backend/managers/whoop_manager.py` | OAuth 2.0 client (auth-code flow), **rotating refresh-token persistence**, paginated v2 API access |
| `backend/modules/whoop_module.py` | Ingestion (cycles + recovery, sleeps, workouts → Firestore) and `describe()` summaries for the coach |
| `backend/api/whoop.py` | `/whoop/authorize`, `/whoop/callback`, `/whoop/status`, `/whoop/sync` |
| `backend/modules/tool_module.py` | New backend tool `query_whoop_data` (metrics: recovery, strain, sleep, workouts) |
| `backend/llm/prompts/tool_module/few_shot_function_calls.txt` | Whoop few-shot examples + autoregulation framing |
| `backend/llm/prompts/tool_module/tool_call_use.txt` | Classifier now triggers on recovery/sleep/readiness topics |
| `backend/main.py` | Router registration + hourly polling job (APScheduler, `whoop_poll_all`) |
| `backend/config.py` | Whoop env vars |
| `backend/tests/smoke_test_whoop.py` | Mock-based smoke test (token rotation, pagination, describe) |

## One-time setup

1. Create an app at https://developer-dashboard.whoop.com (log in with your
   Whoop account; requires active membership).
   - Redirect URI: `http://localhost:5001/whoop/callback` (match `BACKEND_PORT`)
   - Scopes: `offline read:cycles read:sleep read:recovery read:workout read:body_measurement read:profile`
2. Add to your `.env.local` (and `.env.device` if used):

   ```
   WHOOP_CLIENT_ID=...
   WHOOP_CLIENT_SECRET=...
   WHOOP_REDIRECT_URI=http://localhost:5001/whoop/callback
   # optional, defaults to 60
   WHOOP_POLL_INTERVAL_MIN=60
   ```

3. Connect your account: with the backend running and a Firebase ID token in hand,
   `GET /whoop/authorize` (Bearer token) → open the returned `authorize_url` in a
   browser → approve. The callback stores tokens and runs an initial 30-day backfill.

## Data layout (Firestore, under `studies/{STUDY_ID}/users/{uid}/`)

- `integrations/whoop` — access/refresh tokens, expiry, `last_synced_at`
- `whoop-cycles/{id}` — cycle + nested `recovery` (score: recovery %, HRV, RHR)
- `whoop-sleeps/{id}` — sleep records
- `whoop-workouts/{id}` — Whoop-detected workouts

## Design notes / gotchas handled

- **Rotating refresh tokens**: every refresh persists the new token to Firestore
  immediately, and refreshes are serialized per-user with an asyncio lock so
  concurrent requests can't burn the same token twice.
- **Recovery timing**: recovery only exists after a completed sleep; describe()
  says so when empty, and the tool description tells the LLM it's a morning signal.
- **Sync overlap**: each poll re-fetches the last 3 days so PENDING_SCORE →
  SCORED transitions get picked up.
- **No webhooks**: hourly polling only (fine for one user). The job is skipped
  entirely if `WHOOP_CLIENT_ID` is unset.
- **Autoregulation hook for Workstream 2**: `WhoopModule.get_latest_recovery(uid)`
  returns the most recent scored recovery — this is the input for recovery-based
  plan scaling when the strength plan logic is rewritten.

## Verify

```
APP_ENV=local PYTHONPATH=. python3 backend/tests/smoke_test_whoop.py
```

14 checks: token rotation, pagination, unconnected handling, all four describe metrics.
