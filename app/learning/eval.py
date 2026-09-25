"""AUSTRO AI - Deterministic evaluation harness (Phase E).

Runs the learning engine's deterministic algorithms against a fixed golden
dataset and checks the recorded expectations. Everything here is offline and
stateless: no database, no network. Used by tests and by the docs as proof of
behavior (never trust, verify).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Tuple

from app.learning.mastery import MasterySkill
from app.learning.spaced import SpacedReviewScheduler

GOLDEN_MASTERY = [
    {  # path to mastery with mixed evidence kinds (all correct)
        "records": [("assessment", True), ("practice", True), ("quiz", True),
                    ("review", True), ("assessment", True), ("practice", True)],
        "expected_state": "MASTERED",
    },
    {  # high score but a single evidence kind stays below MASTERED
        "records": [("assessment", True), ("assessment", True), ("assessment", True),
                    ("assessment", True), ("assessment", True)],
        "expected_state_any_of": ("LEARNING", "PRACTICING", "NEAR_MASTERY"),
    },
    {  # failure after mastery -> regression
        "records": [("assessment", True), ("quiz", True), ("practice", True),
                    ("assessment", True), ("review", True),
                    ("assessment", False), ("assessment", False), ("assessment", False)],
        "expected_state_any_of": ("REGRESSED", "PRACTICING"),
    },
]

GOLDEN_SCHEDULE = {
    # SM-0 ladder: 1 -> 2 on success.
    "first_success": {"item": {"interval_days": 1}, "correct": True,
                      "expected_interval": 2},
    "long_success": {"item": {"interval_days": 15}, "correct": True,
                     "expected_interval": 30},
    "failure_resets": {"item": {"interval_days": 7}, "correct": False,
                       "expected_interval": 1},
    "ease_up": {"item": {"interval_days": 2, "ease": 2.4}, "correct": True,
                "expected_ease": 2.55},
}

GOLDEN_KIND = [
    {"kind": "assessment", "correct": True, "expected_correct": True},
    {"kind": "review", "correct": False, "expected_correct": False},
]


def _simulate(case: Dict[str, Any]) -> MasterySkill:
    skill = MasterySkill()
    for kind, correct in case["records"]:
        skill.record(correct=correct, kind=kind)
    return skill


def run_golden_mastery() -> Tuple[bool, List[str]]:
    failures: List[str] = []
    for index, case in enumerate(GOLDEN_MASTERY):
        skill = _simulate(case)
        observed = skill.state
        expected = case.get("expected_state")
        accepted = case.get("expected_state_any_of")
        if expected is not None and observed != expected:
            failures.append(f"case {index}: expected {expected}, got {observed}")
        if accepted and observed not in accepted:
            failures.append(f"case {index}: expected one of {accepted}, got {observed}")
    return not failures, failures


def run_golden_schedule(scheduler: SpacedReviewScheduler) -> Tuple[bool, List[str]]:
    failures: List[str] = []
    on = date(2026, 1, 1)
    for label, case in GOLDEN_SCHEDULE.items():
        item = dict(case["item"])
        updated = scheduler.schedule(item, case["correct"], on=on)
        if "expected_interval" in case and \
                updated["interval_days"] != case["expected_interval"]:
            failures.append(f"{label}: interval {updated['interval_days']} != "
                            f"{case['expected_interval']}")
        if "expected_ease" in case:
            got = round(updated["ease"], 2)
            if got != round(case["expected_ease"], 2):
                failures.append(f"{label}: ease {got} != {case['expected_ease']}")
    return not failures, failures


def run_golden_kind() -> Tuple[bool, List[str]]:
    failures: List[str] = []
    skill = MasterySkill()
    for case in GOLDEN_KIND:
        skill.record(correct=case["correct"], kind=case["kind"])
    if set(skill.kinds) != {"assessment", "review"}:
        failures.append(f"distinct kinds {set(skill.kinds)} != set")
    return not failures, failures


def run_all() -> Dict[str, Any]:
    """All-in-one harness used by tests and docs (no fixtures needed)."""
    scheduler = SpacedReviewScheduler()
    results = {
        "mastery": run_golden_mastery(),
        "schedule": run_golden_schedule(scheduler),
        "kinds": run_golden_kind(),
    }
    return {
        "checks": {name: ok for name, (ok, _) in results.items()},
        "failures": {name: errors for name, (_, errors) in results.items()},
        "all_passed": not any(errors for _, errors in results.values()),
    }


__all__ = ["run_all", "run_golden_mastery", "run_golden_schedule",
           "run_golden_kind", "GOLDEN_MASTERY", "GOLDEN_SCHEDULE"]