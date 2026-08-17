from __future__ import annotations

import logging
from typing import Callable

from ulauncher.internals import effects
from ulauncher.internals.query import Query
from ulauncher.internals.result import Result
from ulauncher.modes.clipboard.clipboard_history import get_entries
from ulauncher.modes.clipboard.results import ClipboardResult
from ulauncher.modes.mode import Mode
from ulauncher.utils.eventbus import EventBus

_events = EventBus()
logger = logging.getLogger(__name__)

# Not a real query a user would type -- the Clipboard mode-dot in ulauncher_window.py sends
# this directly via query_changed() to jump straight into clipboard history, the same way the
# Files dot sends "~" to FileBrowserMode.
TRIGGER_QUERY = "ulauncher:clipboard"


class ClipboardMode(Mode):
    def matches_query_str(self, query_str: str) -> bool:
        return query_str == TRIGGER_QUERY

    def handle_query(self, _query: Query, callback: Callable[[effects.EffectMessage], None]) -> None:
        results: list[Result] = [ClipboardResult(entry) for entry in get_entries()]
        callback(effects.render_results(results))

    def activate_result(
        self,
        _action_id: str,
        result: Result,
        _query: Query,
        callback: Callable[[effects.EffectMessage], None],
    ) -> None:
        if not isinstance(result, ClipboardResult):
            logger.error("Unexpected result type for clipboard activation: %s", type(result).__name__)
            callback(effects.do_nothing())
            return
        if result.entry_kind == "image":
            _events.emit("app:copy_and_close_image", result.image_path)
        else:
            _events.emit("app:copy_and_close", result.full_text)
        callback(effects.close_window())
