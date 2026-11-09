# Telegram bot — incident fixes (2026-11-09)

Reported by the bot owner with screenshots of the live chat:

1. Tapping **«📖 ابدأ الدرس»** right after creating a learning goal answered with
   `❌ عذراً، حدث خطأ. الرجاء المحاولة مرة أخرى.` — three times in a row.
2. Several menu buttons rendered but did nothing when tapped (dead
   `callback_data`).

Both are fixed. This document records the root cause, the fix, and how to verify
it, because each one was a *silent* failure: the automated suite was green while
the bot was visibly broken for the user.

---

## 1. `❌ حدث خطأ` on «ابدأ الدرس» — shadowed facade method

### Symptom

From the screenshots: the goal «تعلم اللغة الإنجليزية» and its objectives were
created successfully (`✅ أُنشئ الهدف ... وتم تجهيز المنهج`), then every press of
«ابدأ الدرس» (`callback_data=menu_learn_next`) produced the generic error bubble
and no lesson.

### Root cause

`app/learning/engine.py`, `AdaptiveLearningEngine.__init__`:

```python
self.curricula = CurriculumEngine(store)     # instance attribute
...
def curricula(self, owner_user_id, goal_id):  # facade method, same name
    return self.curricula_.for_goal(...)
```

In Python an **instance attribute wins over a class method of the same name**.
`services.learning_engine.curricula(user_id, goal_id)` therefore raised

```
TypeError: 'CurriculumEngine' object is not callable
```

The exception escaped `menu_learn_next`, python-telegram-bot handed it to the
global `error_handler`, and the user got `public_message()`'s generic fallback.
`menu_learn` had the same call, so the learning hub failed the same way.

### Why the tests missed it

`tests/test_learning.py` used the *sub-engine* directly
(`engine.curricula.ordered_objective_ids(...)`) and `tests/test_rate_limit_guards.py`
replaced `learning_engine` with a `Mock`, whose `curricula` attribute is
callable. Nothing ever called the facade method on a real engine.

### Fix

- Sub-engines are now stored under underscore-suffixed names
  (`curricula_`, `lessons_`, `sessions_`, `planner_`, `review_`, `coach_`,
  `books_`, `flashcards_`, `assessments_`, `misconceptions_`, `diagnostic_`,
  `scheduler_`), so a name can never collide with a facade method again.
- `tests/test_learning_flow_regressions.py::test_no_instance_attribute_shadows_a_facade_method`
  re-derives the shadowing set from `__init__`'s source, so the whole class of
  bug is guarded, not just this one name.
- The handlers that read learning state (`menu_learn`, `_learning_curriculum`)
  now degrade instead of crashing: a failing sub-read is logged and the rest of
  the screen is still rendered.

---

## 2. Dead buttons

Rendered with `callback_data`, but no handler was registered, so Telegram
delivered the tap and the bot ignored it:

| Button | `callback_data` | Now |
| --- | --- | --- |
| ✅ أكملت مهمة (plan) | `plan_complete` | replaced by one button **per task** (`plan_task_<n>`) that toggles completion, updates `progress.tasks_completed`, and re-renders the plan with a progress bar |
| 🎯 تحدي اليوم | `discipline_challenge` | `discipline_challenge` handler: a deterministic micro-challenge derived from the user's real plan/habits/stats |
| 📚 جلسة دراسة | `study_session` | `study_session` + `study_session_topic` (routed through `text_router`) |
| 📖 شرح مفهوم برمجي | `prog_concept` | `prog_concept` + `prog_concept_text` (routed through `text_router`) |
| 📊 إحصائيات مفصلة | `progress_stats` | detailed stats screen (goals, habits, best streak, 7-day log, reviews) |

`tests/test_learning_flow_regressions.py::test_every_button_in_the_bot_has_a_handler`
now parses every `callback_data` literal in the module (f-strings included) and
matches it against the registered patterns exactly like PTB does. A new button
without a handler fails the suite.

---

## 3. Task completion is idempotent (found during the `main` transfer)

Telegram retries a callback query when the bot does not acknowledge it fast
enough, and two workers can handle the same tap concurrently. The first version
of the new "complete task" button flipped whatever was stored, so the *same*
callback delivered twice undid the completion and the weekly total went
`1 → 0` — the user's work disappeared.

Completion is now stated, not flipped:

* the callback names the desired state (`plan_task_<n>` = done,
  `plan_task_<n>_0` = reopened);
* `PlanRepository.set_task_state()` reads and compare-and-sets under the
  repository lock, returning `changed` / `already`;
* `PlanService.set_task_done()` increments `progress.tasks_completed` from the
  `changed` status **only**, so a duplicate delivery is a no-op (and a reopen
  subtracts exactly one, clamped at zero);
* the credit lands on the **plan's own date**, never on today's row.

Verified: 10 sequential deliveries → one `changed`; 8 concurrent threads →
exactly one `changed`; 3 reopens → the counter returns to its previous value.

**No XP system exists in this codebase** — `progress.tasks_completed` (read by
the dashboard, the weekly summary and the coach) is the accounting surface.

### PostgreSQL parity bug found in the same path

The reopen clamp used SQLite's scalar `MAX(a, b)`. PostgreSQL has no such
function (`max()` is an aggregate), so the statement would have failed there
with `function max(integer, integer) does not exist` — invisible on SQLite.
`app/database/dialect.py` now rewrites the two-argument scalar form to
`GREATEST(a, b)` while leaving aggregates untouched. Regression tests assert
every `daily_plans`/`progress` statement survives `translate_query()` with no
`?` placeholder left and no scalar `MAX` un-translated.

## 4. One bad character no longer loses the whole message

`parse_mode="Markdown"` with AI-generated Arabic text is fragile: a single
unbalanced `*`, `_` or `` ` `` makes Telegram reject the *entire* message
(`BadRequest: Can't parse entities`) and the user loses the answer. The same
applied to answers longer than 4096 characters.

`app/telegram/rendering.py` (new) now backs every `reply_text` /
`edit_message_text` in the presentation layer:

- `normalize_markup()` drops unbalanced emphasis markers before sending (and
  **preserves valid escapes** such as `@Mr\_Austro`);
- if Telegram still rejects the formatting, the text is re-sent as plain text
  with `strip_markdown()` — the content is never lost;
- `chunk_text()` splits long answers on paragraph/line/sentence boundaries
  instead of failing on the 4096-character limit;
- an identical edit ("Message is not modified") is a no-op, and a failed edit is
  re-sent as a new message.

---

## Verification

```bash
# the full suite (offline, no Telegram, no AI provider)
.venv/bin/python -m pytest -q

# just the incident regressions (21 tests)
.venv/bin/python -m pytest tests/test_learning_flow_regressions.py -q
```

| change | verification |
| --- | --- |
| learning engine naming | `test_no_instance_attribute_shadows_a_facade_method`, full flow via the real handler graph |
| dead buttons | `test_every_button_in_the_bot_has_a_handler` (parses every `callback_data` in the source) |
| task idempotency | repeated + concurrent delivery tests, transition-only accounting |
| rendering fallback | markup repair, plain-text retry, 4096-char chunking, edit fallback |
| PostgreSQL parity | `translate_query()` checks; live PG covered by CI's Postgres service |

Result: **998 passed, 131 skipped** (the 131 skips are PostgreSQL/Cassandra
integration modules that need a live server; locally 100 %, in CI the Postgres
job runs 25 of them with zero skips).

Additional defects found and fixed while replaying the whole menu graph in a
sandbox (each now has a test):

* the plan screen built its date with local `datetime.now()` while plan
  creation stores the UTC date, so around midnight the "today" screen looked at
  a date that had no plan (`PlanService.today()` now uses the same UTC rule);
* `prog_concept_text` re-entered a callback-only handler and crashed when the
  concept was typed instead of tapped;
* `study_session` had no deterministic local generator, so a deployment without
  a provider key got "service unavailable" instead of a session.

Manual end-to-end check (fake Telegram objects, the real `build_application()`
handler graph) of the exact screenshot sequence —
`/start` → «🧠 رحلة التعلم» → «🎯 هدف تعلم جديد» → title → objectives →
«📖 ابدأ الدرس» → «📝 اختبار سريع» — now renders the lesson and the quiz with
**zero** `حدث خطأ` bubbles.

## If a user still sees `❌ حدث خطأ`

That bubble is `public_message()`'s fallback, which means an exception reached
the global handler. Look at the log line emitted by `error_handler`
(`Update [user=...] caused error: ...` with a traceback) — the message text is
never logged, only the update label.
