from __future__ import annotations

import contextlib
import logging
import os
import time
import uuid
from typing import Any

from gi.repository import Gdk, GdkPixbuf, Gtk

from ulauncher import paths
from ulauncher.data import JsonConf
from ulauncher.gi import Gio, GLib
from ulauncher.utils import scheduling
from ulauncher.utils.environment import IS_X11_COMPATIBLE
from ulauncher.utils.subprocess_utils import run_command

POLL_INTERVAL_SEC = 1.0

logger = logging.getLogger(__name__)

MAX_ENTRIES = 20
# Guard against a pathological giant copy (e.g. an entire log file) bloating the JSON store.
MAX_TEXT_LENGTH = 200_000

_history_file = f"{paths.DATA}/clipboard_history.json"
_images_dir = f"{paths.DATA}/clipboard_images"


class ClipboardHistoryConf(JsonConf):
    # Newest-first. {"id", "kind": "text"|"image", "text", "image_path", "width", "height", "timestamp"}
    entries: list[dict[str, Any]] = []

    @classmethod
    def load(cls, *, force: bool = False) -> ClipboardHistoryConf:
        return super().load(_history_file, force=force)


def get_entries() -> list[dict[str, Any]]:
    return ClipboardHistoryConf.load().entries


def _add_entry(entry: dict[str, Any]) -> None:
    conf = ClipboardHistoryConf.load()
    entries = conf.entries
    # Skip if identical to the most recent entry -- covers our own "copy again" action
    # re-surfacing content that's already at the top of the history on the next poll.
    if entries and entries[0].get("kind") == "text" == entry.get("kind") and entries[0].get("text") == entry["text"]:
        return
    entries.insert(0, entry)
    evicted = entries[MAX_ENTRIES:]
    del entries[MAX_ENTRIES:]
    for old in evicted:
        if old.get("image_path"):
            with contextlib.suppress(OSError):
                os.remove(old["image_path"])
    conf.save(entries=entries)


# Signature of the clipboard content as of the last poll (text itself, or a hash of an image's
# bytes), so an unchanged clipboard doesn't touch the JSON store every poll tick.
_last_seen_signature: str | None = None


def _record_text(text: str | None) -> None:
    global _last_seen_signature  # noqa: PLW0603
    if not text or not text.strip() or text == _last_seen_signature:
        return
    _last_seen_signature = text
    _add_entry({"id": uuid.uuid4().hex, "kind": "text", "text": text[:MAX_TEXT_LENGTH], "timestamp": time.time()})


def _record_image_file(tmp_path: str) -> None:
    """tmp_path already holds PNG bytes on disk; adopt it into _images_dir as a real entry,
    or discard it if it's identical to the last seen image."""
    global _last_seen_signature  # noqa: PLW0603
    try:
        with open(tmp_path, "rb") as f:
            data = f.read()
    except OSError:
        return
    if not data:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        return
    signature = f"image:{hash(data)}"
    if signature == _last_seen_signature:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        return
    _last_seen_signature = signature
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(tmp_path)
    except GLib.Error:
        logger.warning("Could not load captured clipboard image %s", tmp_path)
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        return
    final_path = f"{_images_dir}/{uuid.uuid4().hex}.png"
    os.replace(tmp_path, final_path)
    _add_entry(
        {
            "id": uuid.uuid4().hex,
            "kind": "image",
            "image_path": final_path,
            "width": pixbuf.get_width(),
            "height": pixbuf.get_height(),
            "timestamp": time.time(),
        }
    )


def _clear_signature() -> None:
    global _last_seen_signature  # noqa: PLW0603
    _last_seen_signature = None


# --- X11 path: GTK3's GDK clipboard works normally here. ---


def _record_pixbuf(pixbuf: GdkPixbuf.Pixbuf | None) -> None:
    if pixbuf is None:
        return
    os.makedirs(_images_dir, exist_ok=True)
    tmp_path = f"{_images_dir}/.tmp-{uuid.uuid4().hex}.png"
    try:
        pixbuf.savev(tmp_path, "png", [], [])
    except GLib.Error:
        logger.warning("Could not save clipboard image to %s", tmp_path)
        return
    _record_image_file(tmp_path)


def _on_targets_received(clipboard: Gtk.Clipboard, atoms: list[Gdk.Atom], _data: Any = None) -> None:
    target_names = [atom.name() for atom in atoms]
    if any(name.startswith("image/") for name in target_names):
        clipboard.request_image(lambda _cb, pixbuf, _d=None: _record_pixbuf(pixbuf))
    elif any(name in ("UTF8_STRING", "STRING", "TEXT") or name.startswith("text/plain") for name in target_names):
        clipboard.request_text(lambda _cb, text, _d=None: _record_text(text))
    else:
        _clear_signature()  # clipboard cleared or holds an unsupported type


def _poll_clipboard_x11() -> None:
    Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).request_targets(_on_targets_received)


# --- Wayland path: GTK3's GDK Wayland clipboard backend doesn't round-trip data at all in this
# environment -- verified directly by connecting to Gtk.Clipboard's "owner-change" signal (never
# fires, even for an external wl-copy) and by calling request_targets/request_text/request_image
# right after an external wl-copy (all come back empty/None, alongside a
# "g_array_new_take: assertion 'data != NULL || len == 0' failed" GLib warning suggesting a real
# bug in this GTK/GDK build's Wayland clipboard marshaling). wl-paste talks to the compositor
# directly and round-trips correctly, so shell out to it instead of GDK's own clipboard API. ---


def _poll_clipboard_wayland() -> None:
    run_command(["wl-paste", "--list-types"], _on_wayland_types, lambda _err: _clear_signature())


def _on_wayland_types(stdout: str) -> None:
    types = stdout.splitlines()
    if any(t.startswith("image/") for t in types):
        _fetch_wayland_image()
    elif any(t.startswith("text/") for t in types):
        run_command(["wl-paste", "--no-newline"], _record_text, lambda _err: None)
    else:
        _clear_signature()


def _fetch_wayland_image() -> None:
    os.makedirs(_images_dir, exist_ok=True)
    tmp_path = f"{_images_dir}/.tmp-{uuid.uuid4().hex}.png"
    launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDERR_SILENCE)
    launcher.set_stdout_file_path(tmp_path)
    try:
        proc = launcher.spawnv(["wl-paste", "--type", "image/png"])
    except GLib.Error:
        return

    def on_done(subprocess_: Gio.Subprocess, result: Gio.AsyncResult) -> None:
        with contextlib.suppress(GLib.Error):
            subprocess_.wait_finish(result)
        if not subprocess_.get_successful():
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            return
        _record_image_file(tmp_path)

    proc.wait_async(None, on_done)


_monitoring_started = False


def start_monitoring() -> None:
    """Begin recording every clipboard copy (from any app, not just ours) into a bounded,
    persisted history. Runs for the lifetime of the app process, independent of window
    state, so background copies are still captured while the launcher is hidden."""
    global _monitoring_started  # noqa: PLW0603
    if _monitoring_started:
        return
    _monitoring_started = True
    poll = _poll_clipboard_x11 if IS_X11_COMPATIBLE else _poll_clipboard_wayland
    scheduling.interval(POLL_INTERVAL_SEC, poll)
