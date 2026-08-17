from __future__ import annotations

from typing import Any

from ulauncher.internals.result import Result

_TITLE_LEN = 80  # chars shown on the single-line title (compact rows, or a wrapped preview's first line)
_PREVIEW_LEN = 300  # chars shown in the wrapped preview for long/multi-line copies


class ClipboardResult(Result):
    entry_id: str = ""
    entry_kind: str = "text"
    full_text: str = ""
    image_path: str = ""

    def __init__(self, entry: dict[str, Any]) -> None:
        kind = entry.get("kind", "text")
        if kind == "image":
            width, height = entry.get("width", 0), entry.get("height", 0)
            super().__init__(name=f"Image ({width}×{height})", icon=entry.get("image_path", ""))
        else:
            text = entry.get("text", "").strip()
            lines = text.splitlines() or [""]
            title = lines[0][:_TITLE_LEN] + ("…" if len(lines[0]) > _TITLE_LEN else "")
            if len(lines) > 1 or len(text) > _TITLE_LEN:
                preview = text[:_PREVIEW_LEN] + ("…" if len(text) > _PREVIEW_LEN else "")
                super().__init__(name=title or "(empty)", description=preview, icon="edit-paste-symbolic", wrap=True)
            else:
                super().__init__(name=title or "(empty)", icon="edit-paste-symbolic", compact=True)
        self.entry_id = entry.get("id", "")
        self.entry_kind = kind
        self.full_text = entry.get("text", "")
        self.image_path = entry.get("image_path", "")
