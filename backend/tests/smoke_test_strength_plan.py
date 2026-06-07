"""Smoke test for Workstream 2 (strength plan): models, serialization, prompt
rendering, dialogue YAML validity. Stubs Firebase/LLM; no network needed.

Run from repo root: APP_ENV=local PYTHONPATH=. python3 backend/tests/smoke_test_strength_plan.py
"""
import os
import sys
import types
from datetime import datetime

os.environ.setdefault("APP_ENV", "local")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

# --- stubs so plan_module imports without Firebase/scheduler/OpenAI ---------
class _FakeFM:
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
fm_mod = types.ModuleType("backend.managers.firebase_manager")
fm_mod.FirebaseManager = _FakeFM
sys.modules["backend.managers.firebase_manager"] = fm_mod

class _FakeLLMProvider:
    @staticmethod
    def get_client():
        return object()
llm_mod = types.ModuleType("backend.llm.llm_provider")
llm_mod.LLMProvider = _FakeLLMProvider
sys.modules["backend.llm.llm_provider"] = llm_mod

import pytz
import yaml

from backend.modules.plan_module import Exercise, Workout, WeeklyPlan, WorkoutsByDay, PlanModule, VALID_WORKOUT_TYPES
from backend.llm.prompts import PromptLoader

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name)

# 1. Strength models ---------------------------------------------------------
ex = Exercise(name="Dumbbell Bench Press", sets=3, rep_range="8-12", target_rir=2,
              progression="add reps to top of range, then add weight", notes=None)
w_strength = Workout(
    type="traditional strength training", completed=False, intensity="moderate",
    timeStart="6:30 AM", durationMin=50, isPlanWorkout=True, isHKWorkout=False,
    focus="full_body", exercises=[ex], autoregulation="scale_by_recovery",
)
check("strength workout type valid", w_strength.type in VALID_WORKOUT_TYPES)
check("exercise model holds RIR/progression", w_strength.exercises[0].target_rir == 2)

# Legacy cardio workout with NO strength fields still validates (backward compat)
w_cardio = Workout(type="rowing", completed=False, intensity="light", timeStart="7:00 AM",
                   durationMin=20, isPlanWorkout=True, isHKWorkout=False)
check("legacy workout (no strength fields) validates", w_cardio.focus is None and w_cardio.exercises is None)

plan = WeeklyPlan(
    split_type="full_body", rationale="r", revision=None, isActive=True,
    start="2026-06-07", end="2026-06-13",
    workoutsByDay=WorkoutsByDay(
        Sunday=None, Monday=[w_strength], Tuesday=[w_cardio], Wednesday=[w_strength],
        Thursday=None, Friday=[w_strength], Saturday=None,
    ),
)
check("weekly plan with split_type validates", plan.split_type == "full_body")

# 2. serialize_plan keeps strength fields and converts intensity/time --------
tz = pytz.timezone("America/Los_Angeles")
serialized = PlanModule.serialize_plan(plan.model_dump(), tz)
mon = serialized["workoutsByDay"]["Monday"][0]
check("serialize keeps exercises", mon["exercises"][0]["name"] == "Dumbbell Bench Press")
check("serialize keeps autoregulation", mon["autoregulation"] == "scale_by_recovery")
check("serialize converts intensity enum", mon["intensity"] == "moderate")
check("serialize converts timeStart to ISO", "T06:30" in mon["timeStart"])

# 3. Prompt rendering with Whoop slot ----------------------------------------
prompt = PromptLoader.plan_generation_prompt(
    datetime(2026, 6, 6, 8, 0), "2026-06-07", "2026-06-13",
    history=[], memory="", chat_state="check-in", plan_history="(none)",
    whoop_recovery="2026-06-05 (Fri): recovery 71% , HRV 58.4 ms, RHR 53 bpm",
)
check("prompt: whoop recovery injected", "recovery 71%" in prompt)
check("prompt: no unfilled slots", "{{WHOOP_RECOVERY}}" not in prompt and "{{START_DATE}}" not in prompt)
check("prompt: strength variables present", "Reps In Reserve" in prompt and "Progressive overload" in prompt)
check("prompt: volume landmark replaced CDC", "10–20 weekly sets" in prompt and "150 minutes" not in prompt)
check("prompt: equipment constraints present", "NO barbells" in prompt)
check("prompt: autoregulation tiers present", "scale_by_recovery" in prompt)

# 4. Dialogue YAMLs parse and contain the right hooks ------------------------
def load(p):
    with open(p) as f:
        return yaml.safe_load(f)

a = load("backend/llm/prompts/dialogue_module/check_in/assessment/assessment.yml")
g = load("backend/llm/prompts/dialogue_module/check_in/goal_setting/goal_setting.yml")
o = load("backend/llm/prompts/dialogue_module/onboarding/goal_setting/goal_setting.yml")
check("assessment yml parses + transitions intact", a["transition"]["class_transitions"]["completed"] == "compare_goals")
check("assessment asks about progression", "did you progress" in a["prompt"])
check("assessment uses query_whoop_data", "query_whoop_data" in a["prompt"])
check("checkin goal_setting yml parses + transitions intact", g["transition"]["class_transitions"]["completed"] == "counseling")
check("checkin goal_setting volume landmark", "10-20 working sets" in g["prompt"])
check("onboarding goal_setting yml parses + transitions intact", o["transition"]["class_transitions"]["completed"] == "advice")
check("onboarding asks days/experience/equipment", "days per week" in o["prompt"] and "equipment" in o["prompt"])

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
