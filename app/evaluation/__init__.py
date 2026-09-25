"""AUSTRO AI - Evaluation Framework (Phase F).

Container for golden datasets, evaluation runners, and metrics across
coaching, learning, RAG, memory, and security domains. All evaluators are
deterministic, offline, and property-based (no exact-string LLM comparison).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.ai.telemetry import AIRunRecord


@dataclass
class EvaluationCase:
    """A single evaluation case with input, context, expected properties,
    forbidden behaviors, and optional reference."""
    case_id: str
    name: str
    input: Dict[str, Any]
    context: Dict[str, Any]
    expected_properties: Dict[str, Any]
    forbidden_behaviors: List[str] = field(default_factory=list)
    reference_answer: Optional[str] = None
    grounding_required: bool = True


@dataclass
class EvaluationResult:
    """Result of running a single evaluation case."""
    case_id: str
    name: str
    passed: bool
    properties: Dict[str, Any]
    violations: List[str]
    telemetry: Optional[List[AIRunRecord]] = None


@dataclass
class EvaluationSuite:
    """A collection of evaluation cases with a name and description."""
    name: str
    description: str
    cases: List[EvaluationCase] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def run(self) -> EvaluationResult:
        """Run all cases and return aggregated results."""
        all_passed = True
        all_violations: List[str] = []
        all_telemetry: List[AIRunRecord] = []

        for case in self.cases:
            # Import here to avoid circular issues at module level
            from app.evaluation.runners import run_case
            result = run_case(case)
            if not result.passed:
                all_passed = False
            all_violations.extend(result.violations)
            if result.telemetry:
                all_telemetry.extend(result.telemetry)

        return EvaluationResult(
            case_id=self.name,
            name=self.name,
            passed=all_passed,
            properties={},
            violations=all_violations,
            telemetry=all_telemetry,
        )


# ---------------------------------------------------------------------------
# Golden Datasets
# ---------------------------------------------------------------------------

# Coaching evaluation cases
COACHING_CASES: List[EvaluationCase] = []

# Learning evaluation cases
LEARNING_CASES: List[EvaluationCase] = []

# RAG evaluation cases
RAG_CASES: List[EvaluationCase] = []

# Memory evaluation cases
MEMORY_CASES: List[EvaluationCase] = []

# Security evaluation cases
SECURITY_CASES: List[EvaluationCase] = []


# ---------------------------------------------------------------------------
# Evaluation Suite Registry
# ---------------------------------------------------------------------------

eval_suites: Dict[str, EvaluationSuite] = {}


def register_suite(suite: EvaluationSuite) -> None:
    """Register an evaluation suite by name."""
    eval_suites[suite.name] = suite


def get_suite(name: str) -> Optional[EvaluationSuite]:
    """Retrieve a registered suite by name."""
    return eval_suites.get(name)


def list_suites() -> List[str]:
    """List all registered suite names."""
    return list(eval_suites.keys())


def run_all_suites() -> Dict[str, EvaluationResult]:
    """Run all registered suites and return results."""
    results: Dict[str, EvaluationResult] = {}
    for name, suite in eval_suites.items():
        results[name] = suite.run()
    return results


def run_all() -> Dict[str, Any]:
    """Convenience: run all suites and return the doc-friendly dict.

    Used by docs and CI; mirrors the shape of app.learning.eval.run_all.
    """
    # Lazy registration to avoid circular imports
    _register_default_suites()
    results = run_all_suites()
    return {
        "checks": {name: res.passed for name, res in results.items()},
        "failures": {name: res.violations for name, res in results.items()},
        "all_passed": all(res.passed for res in results.values()),
    }


def _register_default_suites() -> None:
    """Register default evaluation suites. Called lazily to avoid circular imports."""
    if eval_suites:  # Already registered
        return
    from app.evaluation.runners import (
        build_rag_suite,
        build_learning_suite,
        build_memory_suite,
        build_security_suite,
    )
    register_suite(build_rag_suite())
    register_suite(build_learning_suite())
    register_suite(build_memory_suite())
    register_suite(build_security_suite())


# ---------------------------------------------------------------------------
# Runner helper (kept here to avoid circular imports in tests)
# ---------------------------------------------------------------------------

def run_case(case: EvaluationCase) -> EvaluationResult:
    """Execute a single evaluation case.

    Subclasses / modules should implement the logic per domain (RAG, learning,
    memory, security). This stub delegates to the appropriate domain runner.
    """
    from app.evaluation.runners import run_case as _run_case
    return _run_case(case)