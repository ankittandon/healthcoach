# Beebo dialogue FSM

The dialogue module is a state machine. Each state's `prompt` is the coaching task; a
`StateClassifier` (see `dialogue_module/dialogue_classification_prompt*.txt`) judges the
running history against the task and emits `continue` (stay) or `completed` (take the edge
defined in `class_transitions`). State prompts drive tool calls (`generate_plan`,
`query_health_data`). Each turn's *delivery* is chosen separately by the strategy classifier
(11 MI moves), and every generated reply passes through the safety classify→revise guardrail.

## Onboarding flow

```
introduction ──completed──▶ program ──▶ past_experience ──▶ barriers ──▶ motivation
   │  (name, age)            (align        (history w/     (injury/    (why now +
   ▼                          expects,      exercise)       obstacles)   long-term)
 continue⟲                    4-wk, Sun-Sat)    │              │            │
                                               ⟲              ⟲            ▼
                                                                       goal_setting
                                                                    (FITT, one dim at a
                                                                     time → generate_plan,
                                                                     revise until confirmed)
                                                                           │ completed
                                                                           ▼
                                                                         advice
                                                                    (resources, barriers,
                                                                     reframe neg. thoughts,
                                                                     problem-solve)
                                                                           │ completed
                                                                           ▼
                                                                        goodbye
                                                                  (wrap up; "this is the
                                                                   only session")  ⟲ self

   Branches declared on the onboarding root: health_concerns, not_consented
   (not_consented is a stub: "...semi-structured interview. You must ...")
```

Defined order on the `onboarding` root node: `introduction`, `goal_setting`, `completed`, `not_consented`.
(`health_concerns` and `program`/`past_experience`/`barriers`/`motivation`/`advice` exist as
state files wired via each child's `class_transitions`.)

## Check-in flow (weekly, ~10–15 min)

```
introduction ──completed──▶ health_status ──▶ assessment ──▶ compare_goals ──▶ goal_setting
  (purpose +                 (new symptoms?     (FITT recall    (met goal? praise /  (new FITT goal,
   ~10-15 min)                high-risk →        vs. system,     short? explore       generate_plan,
   │                          stop + refer MD)   stage of        barriers)            maintain-or-+1)
   ▼                              │              change)            │                    │ completed
 continue⟲                       ⟲                 │                ⟲                    ▼
                                                   ⟲                                  counseling
                                                                              (pick stage-matched
                                                                               topics: goal-setting,
                                                                               barriers, self-efficacy,
                                                                               relapse prevention, ...)
                                                                                       │ completed
                                                                                       ▼
                                                                                    goodbye
                                                                            (answer Qs, wish luck,
                                                                             next check-in in 1 wk) ⟲ self
```

Root `check_in` node lists child `introduction`; the rest chain via `class_transitions`.

## Per-state transition table

| Flow      | State           | continue →     | completed →    | tools                         |
|-----------|-----------------|----------------|----------------|-------------------------------|
| onboarding| introduction    | introduction   | program        | —                             |
| onboarding| program         | program        | past_experience| —                             |
| onboarding| past_experience | past_experience| barriers       | —                             |
| onboarding| barriers        | barriers       | motivation     | —                             |
| onboarding| health_concerns | health_concerns| motivation     | —                             |
| onboarding| motivation      | motivation     | goal_setting   | —                             |
| onboarding| goal_setting    | goal_setting   | advice         | generate_plan                 |
| onboarding| advice          | advice         | goodbye        | —                             |
| onboarding| goodbye         | goodbye        | goodbye (self) | —                             |
| onboarding| not_consented   | —              | —              | — (stub)                      |
| check_in  | introduction    | introduction   | health_status  | —                             |
| check_in  | health_status   | health_status  | assessment     | —                             |
| check_in  | assessment      | assessment     | compare_goals  | query_health_data             |
| check_in  | compare_goals   | compare_goals  | goal_setting   | —                             |
| check_in  | goal_setting    | goal_setting   | counseling     | generate_plan, query_health_data |
| check_in  | counseling      | counseling     | goodbye        | —                             |
| check_in  | goodbye         | goodbye        | goodbye (self) | —                             |

> Note: `goodbye` self-loops on both `continue` and `completed`, so it is terminal — the
> conversation only ends from `goodbye`, per the response-generation system prompt
> ("end conversation only in the goodbye state").
