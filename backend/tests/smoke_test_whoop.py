"""Smoke test for the Whoop integration: token rotation, pagination, describe().
Stubs Firestore + Whoop HTTP so it runs with no network and no Firebase."""
import asyncio
import os
import sys
import time
import types

os.environ.setdefault("APP_ENV", "local")

# ---------------------------------------------------------------------------
# Fake Firestore
# ---------------------------------------------------------------------------
class FakeDoc:
    def __init__(self, data):
        self._data = data
    @property
    def exists(self):
        return self._data is not None
    def to_dict(self):
        return dict(self._data) if self._data else None

class FakeDocRef:
    def __init__(self, store, path):
        self.store, self.path = store, path
    async def set(self, data, merge=False):
        cur = self.store.get(self.path, {}) if merge else {}
        cur.update(data)
        self.store[self.path] = cur
    async def get(self):
        return FakeDoc(self.store.get(self.path))
    def collection(self, name):
        return FakeColRef(self.store, f"{self.path}/{name}")

class FakeQuery:
    def __init__(self, store, prefix, filters):
        self.store, self.prefix, self.filters = store, prefix, filters
    def where(self, field, op, value):
        return FakeQuery(self.store, self.prefix, self.filters + [(field, op, value)])
    async def stream(self):
        for path, data in list(self.store.items()):
            if not path.startswith(self.prefix + "/"):
                continue
            ok = True
            for field, op, value in self.filters:
                v = data.get(field)
                if v is None:
                    ok = False; break
                if op == ">=" and not v >= value: ok = False; break
                if op == "<=" and not v <= value: ok = False; break
            if ok:
                doc = FakeDoc(data)
                doc.id = path.rsplit("/", 1)[1]
                yield doc

class FakeColRef(FakeQuery):
    def __init__(self, store, prefix):
        super().__init__(store, prefix, [])
    def document(self, doc_id):
        return FakeDocRef(self.store, f"{self.prefix}/{doc_id}")

STORE = {}

class FakeFirebaseManager:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    def get_user_doc_ref(self, uid):
        return FakeDocRef(STORE, f"users/{uid}")
    def get_users_col_ref(self):
        return FakeColRef(STORE, "users")
    async def get_user_timezone(self, uid):
        return "America/Los_Angeles"

fake_fm_module = types.ModuleType("backend.managers.firebase_manager")
fake_fm_module.FirebaseManager = FakeFirebaseManager
sys.modules["backend.managers.firebase_manager"] = fake_fm_module

from backend.managers.whoop_manager import WhoopManager, WhoopNotConnectedError  # noqa: E402
from backend.modules.whoop_module import WhoopModule  # noqa: E402

wm = WhoopManager()
UID = "testuser"
PASS = []
FAIL = []

def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)

async def test_not_connected():
    check("not_connected detection", not await wm.is_connected(UID))
    out = await WhoopModule.describe(UID, "recovery")
    check("describe handles unconnected", "not connected" in out)

async def test_token_rotation():
    # Seed expired tokens
    await wm._persist_tokens(UID, {"access_token": "old_at", "refresh_token": "rt_1", "expires_in": -100})
    calls = []
    async def fake_token_request(payload):
        calls.append(payload)
        assert payload["grant_type"] == "refresh_token"
        assert payload["refresh_token"] == "rt_1"
        return {"access_token": "new_at", "refresh_token": "rt_2", "expires_in": 3600}
    wm._token_request = fake_token_request
    token = await wm.get_valid_access_token(UID)
    stored = STORE[f"users/{UID}/integrations/whoop"]
    check("refresh returns new access token", token == "new_at")
    check("ROTATED refresh token persisted", stored["refresh_token"] == "rt_2")
    check("expiry persisted in future", stored["expires_at"] > time.time())
    # Second call must NOT refresh again (token valid now)
    token2 = await wm.get_valid_access_token(UID)
    check("no redundant refresh", token2 == "new_at" and len(calls) == 1)

async def test_pagination():
    pages = [
        {"records": [{"id": 1}, {"id": 2}], "next_token": "tok2"},
        {"records": [{"id": 3}], "next_token": None},
    ]
    seen_params = []
    async def fake_get(uid, path, params=None):
        seen_params.append(dict(params or {}))
        return pages.pop(0)
    wm.get = fake_get
    records = [r async for r in wm.paginate(UID, "/cycle", {"limit": 25})]
    check("pagination collects all records", [r["id"] for r in records] == [1, 2, 3])
    check("nextToken passed on page 2", seen_params[1].get("nextToken") == "tok2")

async def test_describe():
    STORE[f"users/{UID}/whoop-cycles/c1"] = {
        "id": "c1", "start": "2026-06-05T13:00:00Z", "score_state": "SCORED",
        "score": {"strain": 9.13, "average_heart_rate": 70, "max_heart_rate": 140},
        "recovery": {"score_state": "SCORED",
                     "score": {"recovery_score": 71, "hrv_rmssd_milli": 58.4,
                               "resting_heart_rate": 53, "user_calibrating": False}},
    }
    STORE[f"users/{UID}/whoop-sleeps/s1"] = {
        "id": "s1", "start": "2026-06-05T05:00:00Z", "score_state": "SCORED", "nap": False,
        "score": {"sleep_performance_percentage": 88,
                  "stage_summary": {"total_light_sleep_time_milli": 14_400_000,
                                    "total_slow_wave_sleep_time_milli": 7_200_000,
                                    "total_rem_sleep_time_milli": 4_320_000}},
    }
    STORE[f"users/{UID}/whoop-workouts/w1"] = {
        "id": "w1", "start": "2026-06-05T17:00:00Z", "end": "2026-06-05T17:52:00Z",
        "sport_name": "weightlifting", "score_state": "SCORED",
        "score": {"strain": 8.4, "average_heart_rate": 112},
    }
    rec = await WhoopModule.describe(UID, "recovery", "2026-06-05", "week")
    check("describe recovery", "recovery 71%" in rec and "HRV 58.4 ms" in rec and "RHR 53 bpm" in rec)
    strain = await WhoopModule.describe(UID, "strain", "2026-06-05", "week")
    check("describe strain", "day strain 9.1" in strain)
    sleep = await WhoopModule.describe(UID, "sleep", "2026-06-05", "week")
    check("describe sleep", "7.2h asleep" in sleep and "88%" in sleep)
    wo = await WhoopModule.describe(UID, "workouts", "2026-06-05", "week")
    check("describe workouts", "weightlifting" in wo and "52 min" in wo)
    bad = await WhoopModule.describe(UID, "stepCount")
    check("unknown metric handled", "Unknown Whoop metric" in bad)
    latest = await WhoopModule.get_latest_recovery(UID)
    check("get_latest_recovery (autoregulation hook)", latest and latest["score"]["recovery_score"] == 71)

async def main():
    await test_not_connected()
    await test_token_rotation()
    await test_pagination()
    await test_describe()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)

asyncio.run(main())
