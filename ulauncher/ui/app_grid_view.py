from __future__ import annotations

import logging
from typing import Callable

from gi.repository import Gdk, Gtk, Pango

from ulauncher import app_id
from ulauncher.gi import GioUnix
from ulauncher.modes.apps.app_rankings import AppRankings
from ulauncher.modes.apps.app_result import AppResult
from ulauncher.modes.apps.launch_app import launch_app
from ulauncher.ui.load_icon_surface import load_icon_surface
from ulauncher.utils import scheduling
from ulauncher.utils.settings import Settings

logger = logging.getLogger(__name__)

ICON_SIZE = 56
TILES_PER_ROW = 4
# Measured tile-row pitch (112px tile + 16px row_spacing) -- used by
# ulauncher_window.py to trim the window's height budget by one row.
ROW_HEIGHT_PX = 128


def list_desktop_apps() -> list[AppResult]:
    """Real, launchable desktop applications only.

    Unlike AppMode.get_triggers() (used for search), this doesn't carve out
    gnome-control-center's individual settings panels (Bluetooth, Appearance,
    etc.) -- those are handy to find by typing but don't belong in an
    app-launcher grid, so NoDisplay is enforced with no exception here.
    """
    settings = Settings.load()
    apps: list[GioUnix.DesktopAppInfo] = GioUnix.DesktopAppInfo.get_all()
    results = []
    for app in apps:
        if not app.get_executable() or not app.get_display_name():
            continue
        if not app.get_show_in() and not settings.disable_desktop_filters:
            continue
        if app.get_nodisplay():
            continue
        if app.get_id() == f"{app_id}.desktop":
            continue
        results.append(AppResult(app))
    return sorted(results, key=lambda result: result.name.lower())


class AppGridView(Gtk.ScrolledWindow):
    """All-apps grid for the Applications mode-dot: big icon + name below, 4 per row."""

    def __init__(self, on_launched: Callable[[], None]) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True, kinetic_scrolling=True
        )
        self._on_launched = on_launched
        self.flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            valign=Gtk.Align.START,
            halign=Gtk.Align.FILL,
            max_children_per_line=TILES_PER_ROW,
            min_children_per_line=TILES_PER_ROW,
            row_spacing=16,
            column_spacing=16,
        )
        self.flow.get_style_context().add_class("app-grid")
        self.flow.connect("size-allocate", self._fit_height)
        self.add(self.flow)

    def set_max_height(self, height: int) -> None:
        self.set_property("max-content-height", height)

    def render(self, apps: list[AppResult]) -> None:
        self.flow.foreach(lambda w: w.destroy())
        for app in apps:
            self.flow.add(self._make_tile(app))
        # Screen visibility is a Gtk.Stack page-selection concern (see
        # ulauncher_window.py's content_stack) -- show_all() here just
        # realizes the widgets, it doesn't put this page on screen.
        self.show_all()

    def _fit_height(self, flow: Gtk.FlowBox, allocation: Gdk.Rectangle) -> None:
        """FlowBox doesn't report a usable natural height-for-width to a
        ScrolledWindow with propagate-natural-height on its own (same class
        of quirk ResultsView._fit_results_height works around for wrapped
        labels) -- ask it directly once a real width is known."""
        if allocation.width <= 0:
            return
        needed_height = flow.get_preferred_height_for_width(allocation.width)[1]
        max_height = self.get_property("max-content-height")
        if max_height > 0:
            needed_height = min(needed_height, max_height)
        if abs(needed_height - self.get_min_content_height()) > 1:
            self.set_min_content_height(needed_height)
            scheduling.run_when_idle(self.queue_resize)

    def _make_tile(self, app: AppResult) -> Gtk.Widget:
        icon = Gtk.Image.new_from_surface(
            load_icon_surface(app.icon or "gtk-missing-image", ICON_SIZE, self.get_scale_factor())
        )
        label = Gtk.Label(
            label=app.name,
            ellipsize=Pango.EllipsizeMode.END,
            max_width_chars=14,
            lines=2,
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        label.get_style_context().add_class("app-grid-tile-label")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        box.pack_start(icon, False, False, 0)
        box.pack_start(label, False, False, 0)

        btn = Gtk.Button(receives_default=False)
        style = btn.get_style_context()
        # "flat" is GTK's own no-chrome button variant -- more reliable than
        # overriding background/border/box-shadow property-by-property against
        # whatever the active system theme paints by default (see peachos.css's
        # note on entry:focus for the same class of leak).
        style.add_class("flat")
        style.add_class("app-grid-tile")
        btn.add(box)
        btn.connect("clicked", lambda *_: self._launch(app.app_id))
        return btn

    def _launch(self, app_id: str) -> None:
        AppRankings.load().bump(app_id)
        if not launch_app(app_id):
            logger.error("Could not launch app %s", app_id)
        self._on_launched()
