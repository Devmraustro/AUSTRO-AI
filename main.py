"""
AUSTRO AI - Application shim.

Phase B: the wired Telegram application lives in `app.telegram.main`. This
module re-exports `build_application`, the scheduler hooks and `main()` so
`import main` / `main.build_application()` keep working unchanged (tests,
smoke test, CI, and the `python main.py` entry point).
"""

from app.telegram.main import (  # noqa: F401
    build_application,
    main,
    scheduler_post_init,
    scheduler_post_stop,
)

__all__ = [
    "build_application",
    "scheduler_post_init",
    "scheduler_post_stop",
    "main",
]