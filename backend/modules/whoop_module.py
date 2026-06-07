"""
WhoopModule: ingestion + description of Whoop data for the coaching agent.

Mirrors the HealthKit pattern: data is fanned into Firestore under the user doc
and the coach queries it via the tool_module (`query_whoop_data`). HealthKit and
Whoop are deliberately NOT merged at ingestion time — they live in separate
collections and the coach can query both.

Firestore layout (under studies/{STUDY_ID}/users/{uid}/):
    whoop-cycles/{cycle_id}     cycle record + nested "recovery" (if scored)
    whoop-sleeps/{sleep_id}     sleep record
    whoop-workouts/{workout_id} workout record
    integrations/whoop          OAuth tokens + last_synced_at
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytz

from backend.managers.firebase_manager import FirebaseManager
from backend.managers.whoop_manager import WhoopManager, WhoopNotConnectedError

logger = logging.getLogger(__name__)

firebase_manager = FirebaseManager()
whoop_manager = WhoopManager()

# On each sync, re-fetch this far back so late-arriving scores
# (PENDING_SCORE -> SCORED) and edited records get updated.
SYNC_LOOKBACK_DAYS = 3
# On first-ever sync, backfill this many days of history.
INITIAL_BACKFILL_DAYS = 30


class WhoopModule:

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------
    @staticmethod
    async def sync_user_data(uid: str) -> Dict[str, int]:
        """Pulls cycles (+recoveries), sleeps, and workouts into Firestore.

        Safe to call repeatedly (hourly poll); upserts by record id.
        """
        if not await whoop_manager.is_connected(uid):
            logger.info(f"Whoop not connected for user {uid}; skipping sync")
            return {}

        user_ref = firebase_manager.get_user_doc_ref(uid)
        meta_ref = user_ref.collection("integrations").document("whoop")
        meta_doc = await meta_ref.get()
        meta = meta_doc.to_dict() or {}

        if meta.get("last_synced_at"):
            start_dt = datetime.fromisoformat(meta["last_synced_at"]) - timedelta(days=SYNC_LOOKBACK_DAYS)
        else:
            start_dt = datetime.now(timezone.utc) - timedelta(days=INITIAL_BACKFILL_DAYS)
        start_iso = start_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

        counts = {"cycles": 0, "recoveries": 0, "sleeps": 0, "workouts": 0}

        # Cycles + recovery per cycle
        cycles = await whoop_manager.get_cycles(uid, start=start_iso)
        for cycle in cycles:
            recovery = None
            if cycle.get("score_state") == "SCORED" or cycle.get("end"):
                try:
                    recovery = await whoop_manager.get_recovery_for_cycle(uid, cycle["id"])
                except Exception as e:
                    logger.warning(f"Failed to fetch recovery for cycle {cycle.get('id')}: {e}")
            doc = dict(cycle)
            doc["recovery"] = recovery
            await user_ref.collection("whoop-cycles").document(str(cycle["id"])).set(doc, merge=True)
            counts["cycles"] += 1
            if recovery:
                counts["recoveries"] += 1

        # Sleeps
        for sleep in await whoop_manager.get_sleeps(uid, start=start_iso):
            await user_ref.collection("whoop-sleeps").document(str(sleep["id"])).set(sleep, merge=True)
            counts["sleeps"] += 1

        # Workouts
        for workout in await whoop_manager.get_workouts(uid, start=start_iso):
            await user_ref.collection("whoop-workouts").document(str(workout["id"])).set(workout, merge=True)
            counts["workouts"] += 1

        await meta_ref.set({"last_synced_at": datetime.now(timezone.utc).isoformat()}, merge=True)
        logger.info(f"Whoop sync complete for user {uid}: {counts}")
        return counts

    @staticmethod
    async def sync_all_users() -> None:
        """Polling entry point: syncs every user that has Whoop connected."""
        users_ref = firebase_manager.get_users_col_ref()
        async for user_doc in users_ref.stream():
            try:
                await WhoopModule.sync_user_data(user_doc.id)
            except WhoopNotConnectedError:
                continue
            except Exception as e:
                logger.error(f"Whoop sync failed for user {user_doc.id}: {e}")

    # ------------------------------------------------------------------
    # Querying / description (used by tool_module)
    # ------------------------------------------------------------------
    @staticmethod
    async def _resolve_date_range(uid: str, reference_date: Optional[str],
                                  aggregation_level: str) -> Tuple[datetime, datetime]:
        """Resolves (start, end) datetimes in the user's local timezone."""
        user_tz = pytz.timezone(await firebase_manager.get_user_timezone(uid))
        now_local = datetime.now(user_tz)

        ref = (reference_date or "today").strip().lower()
        if ref == "today":
            ref_date = now_local.date()
        elif ref == "yesterday":
            ref_date = (now_local - timedelta(days=1)).date()
        else:
            try:
                ref_date = datetime.fromisoformat(reference_date).date()  # type: ignore[arg-type]
            except (ValueError, TypeError):
                ref_date = now_local.date()

        if aggregation_level == "month":
            start_date = ref_date - timedelta(days=29)
        elif aggregation_level == "week":
            start_date = ref_date - timedelta(days=6)
        else:
            start_date = ref_date

        start = user_tz.localize(datetime.combine(start_date, datetime.min.time()))
        end = user_tz.localize(datetime.combine(ref_date, datetime.max.time()))
        return start, end

    @staticmethod
    async def _fetch_range(uid: str, collection: str, start: datetime, end: datetime,
                           time_field: str = "start") -> List[Dict[str, Any]]:
        start_iso = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        end_iso = end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        col_ref = firebase_manager.get_user_doc_ref(uid).collection(collection)
        query = col_ref.where(time_field, ">=", start_iso).where(time_field, "<=", end_iso)
        records = [doc.to_dict() async for doc in query.stream()]
        records.sort(key=lambda r: r.get(time_field, ""))
        return records

    @staticmethod
    def _fmt_day(record_start: str, user_tz: pytz.BaseTzInfo) -> str:
        try:
            dt = datetime.fromisoformat(record_start.replace("Z", "+00:00")).astimezone(user_tz)
            return dt.strftime("%Y-%m-%d (%a)")
        except (ValueError, AttributeError):
            return record_start or "unknown date"

    @staticmethod
    async def describe(uid: str, metric: str, reference_date: Optional[str] = None,
                       aggregation_level: str = "day") -> str:
        """Returns a textual summary of Whoop data, in the style of the
        HealthKit describe() outputs the LLM is already primed on."""
        if not await whoop_manager.is_connected(uid):
            return ("Whoop is not connected for this user. They can connect it from the "
                    "app settings; until then, no recovery/strain/sleep data is available.")

        start, end = await WhoopModule._resolve_date_range(uid, reference_date, aggregation_level)
        user_tz = pytz.timezone(await firebase_manager.get_user_timezone(uid))
        header = f"{start.date()} to {end.date()}"

        if metric == "recovery":
            cycles = await WhoopModule._fetch_range(uid, "whoop-cycles", start, end)
            lines = []
            for c in cycles:
                rec = c.get("recovery") or {}
                score = (rec.get("score") or {})
                if rec.get("score_state") == "SCORED" and score:
                    lines.append(
                        f"{WhoopModule._fmt_day(c.get('start'), user_tz)}: "
                        f"recovery {score.get('recovery_score', '?')}% , "
                        f"HRV {round(score.get('hrv_rmssd_milli', 0), 1)} ms, "
                        f"RHR {score.get('resting_heart_rate', '?')} bpm"
                        + (" (calibrating)" if score.get("user_calibrating") else "")
                    )
            if not lines:
                return f"{header}: no scored Whoop recovery data available. Recovery only appears after a completed sleep."
            return f"Whoop recovery, {header}:\n" + "\n".join(lines)

        if metric == "strain":
            cycles = await WhoopModule._fetch_range(uid, "whoop-cycles", start, end)
            lines = []
            for c in cycles:
                score = c.get("score") or {}
                if c.get("score_state") == "SCORED" and score:
                    lines.append(
                        f"{WhoopModule._fmt_day(c.get('start'), user_tz)}: "
                        f"day strain {round(score.get('strain', 0), 1)} (0-21 scale), "
                        f"avg HR {score.get('average_heart_rate', '?')} bpm, "
                        f"max HR {score.get('max_heart_rate', '?')} bpm"
                    )
            if not lines:
                return f"{header}: no scored Whoop strain data available."
            return f"Whoop strain, {header}:\n" + "\n".join(lines)

        if metric == "sleep":
            sleeps = await WhoopModule._fetch_range(uid, "whoop-sleeps", start, end)
            lines = []
            for s in sleeps:
                if s.get("nap"):
                    continue
                score = s.get("score") or {}
                stage = score.get("stage_summary") or {}
                total_ms = (stage.get("total_light_sleep_time_milli", 0)
                            + stage.get("total_slow_wave_sleep_time_milli", 0)
                            + stage.get("total_rem_sleep_time_milli", 0))
                hours = round(total_ms / 3_600_000, 1) if total_ms else None
                perf = score.get("sleep_performance_percentage")
                if s.get("score_state") == "SCORED":
                    lines.append(
                        f"{WhoopModule._fmt_day(s.get('start'), user_tz)}: "
                        + (f"{hours}h asleep" if hours is not None else "duration unknown")
                        + (f", sleep performance {perf}%" if perf is not None else "")
                    )
            if not lines:
                return f"{header}: no scored Whoop sleep data available."
            return f"Whoop sleep, {header}:\n" + "\n".join(lines)

        if metric == "workouts":
            workouts = await WhoopModule._fetch_range(uid, "whoop-workouts", start, end)
            lines = []
            for w in workouts:
                score = w.get("score") or {}
                name = w.get("sport_name") or f"sport_id {w.get('sport_id', '?')}"
                try:
                    start_dt = datetime.fromisoformat(w["start"].replace("Z", "+00:00"))
                    end_dt = datetime.fromisoformat(w["end"].replace("Z", "+00:00"))
                    duration_min = round((end_dt - start_dt).total_seconds() / 60)
                except (KeyError, ValueError):
                    duration_min = "?"
                lines.append(
                    f"{WhoopModule._fmt_day(w.get('start'), user_tz)}: {name}, "
                    f"{duration_min} min, strain {round(score.get('strain', 0), 1)}, "
                    f"avg HR {score.get('average_heart_rate', '?')} bpm"
                )
            if not lines:
                return f"{header}: no Whoop workouts recorded."
            return f"Whoop workouts, {header}:\n" + "\n".join(lines)

        return f"Unknown Whoop metric '{metric}'. Valid options: recovery, strain, sleep, workouts."

    @staticmethod
    async def get_latest_recovery(uid: str) -> Optional[Dict[str, Any]]:
        """Most recent scored recovery — the autoregulation hook for plan logic
        (Workstream 2): green -> train as planned, red -> cut volume / swap to
        a light or mobility day."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=7)
        cycles = await WhoopModule._fetch_range(
            uid, "whoop-cycles",
            start.replace(tzinfo=timezone.utc), end.replace(tzinfo=timezone.utc),
        )
        for c in reversed(cycles):
            rec = c.get("recovery") or {}
            if rec.get("score_state") == "SCORED" and rec.get("score"):
                return rec
        return None
