"""Textual TUI for running and browsing Cairn pipelines.

The package is split for clarity:

- `render.py`   — trace → styled Text helpers
- `widgets.py`  — custom widgets (ChoicePanel, ConfirmPanel)
- `messages.py` — Message subclasses posted from the worker thread
- `sinks.py`    — TuiSink (events) and TuiInteractionSink (typed requests)
- `app.py`      — the unified `CairnApp`

The public surface is `run_app` / `browse` plus the `CairnApp` class.
"""

from __future__ import annotations

from typing import Any

from .app import CairnApp


def run_app(entry_fn: Any, store_path: str = ".cairn", label: str = "main") -> None:
    app = CairnApp(store_path, entry_fn=entry_fn, label=label)
    app.run()


def browse(store_path: str = ".cairn") -> None:
    app = CairnApp(store_path)
    app.run()


__all__ = ["CairnApp", "run_app", "browse"]
