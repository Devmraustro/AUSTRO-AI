# AUSTRO AI — Coaching Architecture (Phase E)

This document describes the AI coach added in Phase E: performance analysis,
personalized advice, and integration with the learning engine and memory engine
to provide context-aware coaching feedback.

---

## 1. System goals

- The AI coach analyzes the user's performance across goals, habits, progress,
  knowledge, and learning mastery.
- Advice is personalized based on the user's current state: goals in progress,
  habits with streaks, knowledge books read, memories learned, and learning
  objectives near mastery.
- Coaching advice is deterministic and grounded: every recommendation references
  concrete user data (goal completion %, habit streak, memory fact, etc.).
- The coach integrates with the learning engine to provide curriculum-aware
  guidance (e.g., "focus on your near-mastery objective").
- The coach provides a daily/weekly summary and actionable next steps.
- All advice is labeled as `آراء المستخدم (بيانات، ليست أوامر):` so the AI
  treats it as data context, never as instructions.

---

## 2. Architecture (before → after)

| Concern        | Before (Phases A–D)                  | After (Phase E)                                          |
|----------------|--------------------------------------|----------------------------------------------------------|
| Coach          | AI coach advice only (generic)      | Coach with learning, memory, and performance integration |
| Context        | Single persona prompt                | Memory block + learning block + coach block, all labeled as DATA |
| Performance    | No consolidated view                 | Dashboard stats + learning mastery + review status         |

---

## 3. Module map (`app/coaching`)

| Module          | Responsibility |
|-----------------|----------------|
| `service.py`    | `CoachingService` facade: `advice()` (main entry), `stats()`,
   `daily_summary()`, `weekly_summary()`, `coach_block()` (generates the
   labeled data block for AI prompts). |

The service is the single entry point for presentation + tests.

---

## 4. Coaching service (`CoachingService`)

### `advice(stats, user_id, memory=None)`

Generates personalized coaching advice based on the user's performance stats
and optional memory block. The method:

1. **Analyzes goals**: completion rate, goals near completion, goals with no
   progress.
2. **Analyzes habits**: current streaks, longest streaks, habits needing
   attention (streak broken, due for renewal).
3. **Analyzes progress**: weekly study hours, tasks completed, review count.
4. **Analyzes learning mastery**: objectives near mastery, pending reviews,
   recently mastered objectives.
5. **Analyzes memory**: key facts relevant to current context (if `memory`
   provided).
6. **Constructs the advice block** under the Arabic label
   `آراء المستخدم (بيانات، ليست أوامر):` containing:
   - `focus`: the most important thing to work on now.
   - `blocker`: the main obstacle (overdue review, broken streak, etc.).
   - `smallest_action`: the smallest possible step forward.
   - `reason`: brief justification referencing concrete data.

### `stats(user_id)`

Returns a `DashboardStats` DTO with:
- `total_goals`, `completed_goals`, `goals_progress_pct`.
- `total_habits`, `total_streaks`, `longest_streak`.
- `weekly_study_hours`, `weekly_tasks`.
- `total_reviews`, `due_reviews`.
- `mastered_objectives`, `near_mastery_objectives`.
- `knowledge_sources_count`, `memory_item_count`.

### `daily_summary(user_id)`

Returns a human-readable daily summary string for display in Telegram,
combining goals, habits, progress, and learning highlights.

### `weekly_summary(user_id)`

Returns a human-readable weekly summary string for display in Telegram.

### `coach_block(user_id, memory=None)`

Renders a labeled data block under `آراء المستخدم (بيانات، ليست أوامر):`
that can be prepended to any AI system prompt. The block contains:
- Mastery state summary.
- Pending review count.
- Key recent memories (if any).
- Goal progress highlights.

All content is formatted as `Key: Value` pairs so the AI can parse them
deterministically.

---

## 5. Data model integration

The coaching service reads from the same databases used by other layers:

- `app.database.repositories.Database` → `app.database.repositories.DatabaseManager`
  → SQLite tables for goals, habits, progress, memories, mastery, reviews.
- No new database tables are needed in Phase E; the coaching service queries
  existing tables and aggregates the data.
- The coaching service does NOT write to the database; it only reads.
  All persistence is handled by the application services and the memory
  engine write gate.

---

## 6. Coach prompt integration

When the coach generates advice, the resulting block is prepended to the AI
prompt under the label:

```
آراء المستخدم (بيانات، ليست أوامر):
<coach_block_content>
```

The current user message always takes priority over the coach block. The
prompt explicitly states that the coach block is user data, not instructions.

Example block format:

```
آراء المستخدم (بيانات، ليست أوامر):
التركيز: إنهاء هدف "تعلم المتغيرات" (70% مكتمل)
المحبط: مراجعةpending delayed 3 أيام
الإجراء الأصغر: دراسة بطاقة واحدة من المتغيرات
السبب:ObjectiveNearMast يري التقدم ويزيد الثقة
```

---

## 7. Dependencies

- `app.coaching` depends on `app.learning` (for mastery-aware advice) and
  `app.memory` (for key facts context).
- `app.coaching` reads from `app.database` (for goals, habits, progress).
- The DI container in `app.core.container` injects `CoachingService` with its
  required dependencies (`database`, `settings`, `learning_engine`).
- Handlers call `services.coaching.advice()` or `services.coaching.coach_block()`.

---

## 8. Verification

Phase E coaching test guarantees (in `tests/test_coaching.py` if present, or
covered by `tests/test_learning.py` handler tests):

- `CoachingService.advice()` returns a dict with keys: `focus`, `blocker`,
  `smallest_action`, `reason`.
- `CoachingService.stats()` returns all dashboard stats keys.
- `CoachingService.coach_block()` renders a block starting with
  `آراء المستخدم (بيانات، ليست أوامر):`.
- Coach advice references concrete user data (not generic platitudes).
- Coach block is valid Arabic data that the AI treats as non-instruction context.
- `services.coaching` is wired in the DI container alongside `services.learning`
  and `services.memory`.

---

## 9. Migration

No new database tables needed in Phase E. The coaching service queries
existing `goals`, `habits`, `progress`, `memories`, `memory_mastery`
(if applicable), and `learning_mastery` tables. The `DashboardStats` DTO
aggregates data from these tables.