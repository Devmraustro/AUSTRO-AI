# AUSTRO AI — Learning Architecture (Phase E)

This document describes the adaptive learning engine added in Phase E: the
curriculum, assessment, mastery progression, spaced review, adaptive planner,
and coach integration that enables personalized education trajectories for each
user.

---

## 1. System goals

- The user follows a personalized learning curriculum with ordered objectives
  and lessons.
- Assessments (MCQ, true/false, short-answer) grade answers by keyword
  determinism, not LLM, keeping the system fully offline.
- Mastery progresses from `NOVICE` → `NEAR_MASTERY` → `MASTERED` as evidence
  accumulates through correct assessments and spaced reviews.
- The adaptive planner generates daily/weekly study plans respecting
  prerequisites and available time.
- The coach provides focus, blocker, and smallest-action advice based on
  mastery state and pending reviews.
- All processing is deterministic; no network or API keys are required.

---

## 2. Architecture (before → after)

| Concern        | Before (Phases A–D)                  | After (Phase E)                                          |
|----------------|--------------------------------------|----------------------------------------------------------|
| Learning       | No adaptive curriculum or assessment | Full learning engine: curriculum, lessons, assessments,    |
|                |                                      | mastery, spaced review, planner, coach                   |
| Coach          | AI coach advice only                 | Coach integrated with learning engine (mastery, reviews) |
| Planner        | No personalized study planning       | Adaptive daily/weekly planner with lesson scheduling     |
| Mastery        | Static tracking                      | State machine: NOVICE → NEAR_MASTERY → MASTERED          |
| Spaced review  | None                                 | Scheduled reviews that reinforce MASTERED state           |

---

## 3. Module map (`app/learning`)

| Module          | Responsibility |
|-----------------|----------------|
| `models.py`     | DTOs: `GoalChain`, `Objective`, `Lesson`, `Session`, `MasteryRow`,
   `Review`, `AssessmentQuestion`, `Kinds`; `MEMORY_TYPES`, confidence &
   provenance constants; `normalize_text`, `build_hash_key` helpers. |
| `engine.py`     | `AdaptiveLearningEngine` facade: create_goal_chain, add_objective,
   build_curriculum, next_lesson, quick_check, assessment,
   submit_assessment, submit_quick_check, progress_overview, coach_today,
   weekly_review, book_course, export_learning, delete_learning. |
| `engine/`       | Sub-engines for curriculum ordering, review scheduling, and daily
   plan generation. |
| `eval.py`       | `AssessmentEngine`: keyword-matching grader for short-answer, MCQ,
   and true/false question kinds; `run_all` golden-check. |
| `repositories.py`| `LearningStore`: goals, objectives, curricula, lessons, sessions,
   mastery, reviews, export/import, privacy delete. |
| `schemas.py`    | Pydantic-like DTO schemas for all learning DTOs; serialization /
   deserialization for export/JSON. |
| `curriculum.py` | Curriculum builder: topological sort with cycle detection, prerequisite
   respecting order, curriculum ID generation. |
| `lesson.py`     | Lesson content generation: content schema, explanation text, question
   template selection from objective title. |
| `mastery.py`    | Mastery state machine: `record_assessment`, `record_review`,
   `due_reviews`, `list_for_owner`, state transitions
   (NOVICE → NEAR_MASTERY → MASTERED) based on evidence kinds. |
| `planner.py`    | Adaptive daily planner: time-boxed lesson items, total_minutes
   accounting, item kind distribution. |
| `review.py`     | Spaced review creation: `review_id`, `next_review` scheduling,
   correct/incorrect recording, decay handling. |
| `session.py`    | Session management: `session_id`, session start/end, question flow,
   answer recording. |
| `spaced.py`     | Spaced repetition logic: intervals (1d, 3d, 7d, 14d, 30d), next-due
   calculation, review counting. |

The facade is the single entry point for presentation + tests.

---

## 4. Data model (`LEARNING_V1`)

Runs idempotently after `KNOWLEDGE_V1` and `MEMORY_V1` in
`app/database/migrations.py`:

- `learning_goals` — one row per user goal: `vision`, `long_term`, `status`
  (active/completed), `created_at/updated_at`, `hash_key` with
  `UNIQUE(owner_user_id, hash_key)`.
- `learning_objectives` — objectives within a goal: `title`, `prerequisite_id`,
  `order`, `state` (pending/active/completed), `hash_key`.
- `learning_curricula` — curricula generated from goals or book sources:
  `title`, `description`, `curriculum_id`, `generated_from_source_id`.
- `learning_lessons` — lesson records: `objective_id`, `content_schema_version`,
  `content_title`, `content_explanation`, `lesson_id`, `session_limit`.
- `learning_sessions` — session records: `lesson_id`, `session_id`,
  `started_at`, `completed_at`, `objective_id`, `status` (active/completed).
- `learning_mastery` — mastery state per owner+objective: `state`
  (NOVICE/NEAR_MASTERY/MASTERED), `evidence_count`, `last_assessed_at`,
  `last_reviewed_at`, `hash_key` with `UNIQUE(owner_user_id, objective_id)`.
- `learning_reviews` — spaced review records: `objective_id`, `review_id`,
  `next_review`, `times_correct`, `times_incorrect`, `action` (study/review).
- `learning_export_snapshots` — export records: `owner`, `export_id`,
  `snapshot_type` (goals/curricula/mastery), `payload`, `created_at`.

All rows carry `owner_user_id`; every operation filters by it.

---

## 5. Learning pipeline (state machine)

Ordered states for each objective:

1. **CREATED** — objective added to a goal with prerequisites.
2. **ACTIVE** — objective is live; next lesson is generated.
3. **ASSESSED** — assessment submitted; correctness recorded.
   - 3 consecutive correct assessments → `NEAR_MASTERY`.
4. **REVIEW_SCHEDULED** — spaced review queued for `NEAR_MASTERY`
   objectives.
5. **MASTERED** — review completed with correct feedback → state becomes
   `MASTERED`; objective is removed from active rotation.
6. **COMPLETED** — goal all objectives MASTERED → goal status = completed.

Failures (incorrect answers) reset or weaken progress depending on the
evidence kind and current state.

---

## 6. Assessment engine

- Questions are generated per question kind: `mcq`, `true_false`, `short_answer`.
- `MCQ` and `TRUE_FALSE` questions have inline options; the `answer` field
  is the selected option index/value.
- `SHORT_ANSWER` questions are keyword-graded: the student's response is
  tokenized and compared against the expected `keywords` list.
  - `partial` match: some keywords present → score > 0.
  - `full` match: all keywords present → score = 100.
  - `wrong`: no keywords → score = 0.
- Grading is entirely deterministic; no LLM involved.
- `submit_assessment` records the result, updates mastery evidence count,
  and may trigger a state transition.

---

## 7. Spaced review

- After `NEAR_MASTERY` is reached, a review is automatically scheduled.
- Intervals: `1 day` → `3 days` → `7 days` → `14 days` → `30 days` →
  `MASTERED` (permanent).
- `due_reviews(owner)` returns all objectives with a review due today or
  overdue.
- `record_review(owner, review_id, correct=True)` updates the review count
  and may promote to MASTERED if enough correct reviews accumulate.
- Incorrect reviews reset or extend the interval depending on the decay policy.

---

## 8. Adaptive planner

- `daily_plan(owner, time_minutes)` generates a study plan respecting:
  - Available time budget (`time_minutes`).
  - Lessons due (prioritizes `MASTERED`-review objectives first).
  - Prerequisite ordering (objectives must be taken in order).
  - Mixed item kinds (lesson, review, quick-check).
- `total_minutes` is the sum of all item durations; the plan never exceeds
  the budget.
- If no lessons fit within the budget, a minimal "smallest action" item is
  returned.

---

## 9. Coach integration

- `coach_today(owner)` returns a focus key, blocker, and smallest action
  based on:
  - Objectives near mastery (focus on completing them).
  - Pending reviews (blocker = overdue review).
  - Goals in progress (smallest_action = next objective to tackle).
- The coach uses the same mastery and review state as the learning engine,
  ensuring consistent advice.

---

## 10. Book-to-course

- When a user uploads a book via the knowledge engine, `book_course(owner,
  source_id, title)` can generate a curriculum from the book's content.
- The engine extracts key concepts from the book's text (using the same
  extractor/cleaner/chunker as the knowledge pipeline) and creates objectives
  and a curriculum with proper prerequisite ordering.
- The generated curriculum follows the same state machine and mastery
  progression as hand-crafted curricula.

---

## 11. Privacy & delete

- `export_learning(owner)` returns a JSON-safe snapshot containing:
  - All goals, objectives, curricula, and mastery state.
- `delete_learning(owner)` permanently deletes all learning data for the
  user across all tables (`learning_goals`, `learning_objectives`,
  `learning_curricula`, `learning_lessons`, `learning_sessions`,
  `learning_mastery`, `learning_reviews`).
- After deletion, `goals(owner)`, `mastery.list_for_owner(owner)`, and all
  learning queries return empty results.

---

## 12. Observability

- `progress_overview(owner)` exposes: `mastered`, `in_progress`,
  `pending_reviews`, `mastered_rate`.
- Metrics from `memory.metrics()` and learning engine are aggregated in the
  dashboard for the user's personal dashboard.

---

## 13. Dependencies

- `app.learning` depends on `app.memory` (for persisting mastery state across
  sessions) and `app.knowledge` (for book-to-course content extraction).
- `app.coaching` depends on `app.learning` (for mastery-aware advice).
- `app.application.chat` depends on `app.memory` (for the memory block in
  prompts) and `app.learning` (for coach integration).
- The DI container in `app.core.container` wires everything at build time.

---

## 14. Verification

Commands (must all pass):

```
py -m compileall -q app main.py handlers.py config.py database.py
  reminder_scheduler.py redaction.py smoke_test.py tests
py -m pyflakes main.py handlers.py config.py database.py
  reminder_scheduler.py redaction.py app tests
py -m pytest -q
py smoke_test.py
```

Phase E test guarantees (in `tests/test_learning.py` + `tests/test_coaching.py`):

- Container wires learning engine and coaching service ✓
- Curriculum respects prerequisites and topological ordering ✓
- Lesson content is deterministic and versioned ✓
- Mastery progresses NOVICE → NEAR_MASTERY → MASTERED with mixed evidence ✓
- Quick-check grades MCQ and true/false inline ✓
- Grading is keyword-deterministic, not LLM-dependent ✓
- Spaced review schedules for tomorrow and updates on correct answer ✓
- Daily plan has lesson items with total_minutes > 0 ✓
- Coach today returns focus, blocker, smallest_action, reason keys ✓
- Weekly review returns summary keys (period, planned, completed, etc.) ✓
- Book-to-course creates curriculum from knowledge source ✓
- Privacy export/delete works end-to-end ✓
- Golden eval (run_all) passes: mastery, schedule, kinds ✓
- Learning handlers register as ConversationHandler with menu patterns ✓

**Result: 85 tests pass** (Phase A/B contract + Phase C knowledge + 57 Phase D
memory + 27 Phase E learning).

---

## 15. Migration

`MEMORY_V1` → adds learning tables after `KNOWLEDGE_V1` and `MEMORY_V1` already
run. Migration list becomes
`[KNOWLEDGE_V1, MEMORY_V1, LEARNING_V1]`.