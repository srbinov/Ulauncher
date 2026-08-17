from __future__ import annotations

import logging
import os
import sys
import time
from typing import TYPE_CHECKING, Any, cast

from gi.repository import Gdk, Gtk

from ulauncher import paths
from ulauncher.internals.results_update import ResultsUpdate
from ulauncher.ui.app_grid_view import ROW_HEIGHT_PX, AppGridView, list_desktop_apps
from ulauncher.ui.helpers import layer_shell
from ulauncher.ui.helpers.monitor import get_monitor, get_monitor_geometries
from ulauncher.ui.helpers.theme import Theme
from ulauncher.ui.load_icon_surface import load_icon_surface
from ulauncher.ui.results_view import ResultsView
from ulauncher.utils import scheduling
from ulauncher.utils.environment import DESKTOP_ID, IS_X11_COMPATIBLE
from ulauncher.utils.settings import Settings

if TYPE_CHECKING:
    from cairo import ImageSurface
    from gi.repository import GdkPixbuf

    from ulauncher.ui.app import UlauncherApp

logger = logging.getLogger(__name__)


def _tint_icon_surface(surface: ImageSurface, rgba: tuple[float, float, float, float]) -> ImageSurface:
    """Recolor a full-color icon surface to a flat tint using its own alpha channel as a
    mask -- i.e. fake GTK's "symbolic icon" recoloring for a real symbolic icon-name, our
    applications-mode.svg is a full-color raster, so the theme's `color` CSS property (which
    is what recolors folder-symbolic/edit-paste-symbolic to match) has no effect on it."""
    import cairo

    tinted = cairo.ImageSurface(cairo.FORMAT_ARGB32, surface.get_width(), surface.get_height())
    tinted.set_device_scale(*surface.get_device_scale())
    ctx = cairo.Context(tinted)
    ctx.set_source_surface(surface, 0, 0)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_IN)
    ctx.set_source_rgba(*rgba)
    ctx.paint()
    return tinted


class UlauncherWindow(Gtk.ApplicationWindow):
    _css_provider: Gtk.CssProvider | None = None
    layer_shell_enabled = False
    settings: Settings

    def __init__(self, **kwargs: Any) -> None:  # noqa: PLR0915
        logger.info("Opening Ulauncher window")
        self.settings = Settings.load(force=True)
        width_request = self.settings.base_width
        height_request = -1

        if DESKTOP_ID == "GNOME" and not IS_X11_COMPATIBLE and (layout_size := self.get_layout_size()):
            # Give the window the size of a monitor, so the visible content can be positioned
            # within it using margins. Needed because Gnome Wayland gives no control over where
            # the window goes, it only centers it on the monitor Mutter picks.
            width_request = layout_size.width
            height_request = layout_size.height

        super().__init__(
            decorated=False,
            deletable=False,
            has_focus=True,
            icon_name="peachysearch",
            opacity=0,  # set to 0 so we can show the window and get keyboard input before it's fully loaded
            resizable=False,
            skip_pager_hint=True,
            skip_taskbar_hint=True,
            # UTILITY (not the default NORMAL) is what actually keeps this out of the dock's
            # running-apps display -- skip_taskbar_hint alone covers legacy X11 taskbars, but
            # GNOME Shell's own app/window tracking (which dash2dock-lite and every other dock
            # extension build on) keys off the window type, not that hint, to decide whether a
            # window counts as "the app is running" for dock purposes.
            type_hint=Gdk.WindowTypeHint.UTILITY,
            title="peachySearch",
            urgency_hint=True,
            window_position=Gtk.WindowPosition.CENTER,
            width_request=width_request,
            height_request=height_request,
            **kwargs,
        )
        # avoid checking layer shell support for known cases it does not apply (for performance reasons)
        if not IS_X11_COMPATIBLE and DESKTOP_ID != "GNOME" and self.settings.layer_shell and layer_shell.is_supported():
            self.layer_shell_enabled = layer_shell.enable(self)
            if self.layer_shell_enabled:
                logger.info("Layer shell support is enabled")
            else:
                logger.warning(
                    "Layer shell is not supported. If you have issues with window positioning, "
                    "ensure that your compositor supports it and that you have installed the gtk-layer-shell library"
                )

        # Widget structure
        #
        # frame (positioning container for Gnome, not affected by theme)
        # └── shadow_container (provides space for shadow when enabled)
        #     └── theme_root(.app)
        #         ├── prompt_container
        #         │   └── prompt
        #         │       ├── prompt_input (.input)
        #         │       └── prefs_btn (.prefs-btn)
        #         └── results_view (ScrolledWindow)
        #             └── results box (.result-box)
        #                 └── ResultWidget (multiple)

        self.frame = Gtk.Box(valign=Gtk.Align.START)
        self.add(self.frame)

        shadow_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=self._get_shadow_size())
        self.frame.pack_start(shadow_container, True, True, 0)

        self.theme_root = Gtk.Box(app_paintable=True, orientation=Gtk.Orientation.VERTICAL)
        shadow_container.pack_start(self.theme_root, True, True, 0)

        self.prompt = Gtk.Box(spacing=12)
        prompt_container = Gtk.EventBox()
        prompt_container.add(self.prompt)

        self.prompt_input = Gtk.Entry(
            can_default=True,
            can_focus=True,
            has_focus=True,
            is_focus=True,
            height_request=30,
            margin_top=15,
            margin_bottom=15,
            margin_start=20,
            margin_end=20,
            receives_default=True,
            primary_icon_name="edit-find-symbolic",
            primary_icon_sensitive=False,
            primary_icon_activatable=False,
        )

        # A hand-rolled placeholder instead of GtkEntry's native placeholder-text:
        # that property rendered nothing at all in this theme/GTK combo (not just
        # low-contrast -- fully absent), and a real Label gives full control over
        # the liquid-glass color plus lets on_mode_dot_clicked swap the text to
        # "Applications"/"Files". A bare Label has no GdkWindow, so
        # clicks fall through to the entry underneath -- no event handling needed.
        self._default_placeholder = "peachySearch"
        self.placeholder_label = Gtk.Label(
            label=self._default_placeholder,
            halign=Gtk.Align.START,
            valign=Gtk.Align.CENTER,
            margin_start=57,
            can_focus=False,
        )
        self.placeholder_label.get_style_context().add_class("placeholder-label")
        self.prompt_input_overlay = Gtk.Overlay()
        self.prompt_input_overlay.add(self.prompt_input)
        self.prompt_input_overlay.add_overlay(self.placeholder_label)

        self.prefs_btn = Gtk.Button(
            name="prefs_btn",
            width_request=24,
            height_request=24,
            receives_default=False,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            margin_end=15,
        )

        # Idle-state mode picker (Applications / Files), macOS Spotlight style.
        # Dissolves as soon as there's a query -- see on_input_changed.
        # Clipboard mode-dot removed 2026-08-17: its background clipboard poll destabilized
        # Mutter (see ulauncher/modes/clipboard/clipboard_history.py's start_monitoring, which
        # app.py no longer calls). The mode/backend code is still there, just not wired into the
        # UI, so it can come back once the polling approach is fixed and verified safe.
        self._dissolve_timer: scheduling.Context | None = None
        # Which mode's icon+name is currently shown in the search bar in place of the
        # magnifying glass (None = idle/typing state). See on_mode_dot_clicked/_revert_active_mode.
        self._active_mode: str | None = None
        # Loaded lazily in apply_styling(), once the display scale factor is known.
        self._apps_icon_pixbuf: GdkPixbuf.Pixbuf | None = None
        # grab_focus_without_selecting() below fires the entry's own focus-in-event just like a
        # real click would -- without this guard, on_prompt_focus_in would immediately revert the
        # mode this same click is trying to set. See on_mode_dot_clicked.
        self._suppress_focus_revert = False
        self.mode_dots = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
        self.mode_dots.get_style_context().add_class("mode-dots-row")
        # icon-name for the entry's primary icon per mode; "apps" is swapped for the real
        # applications-mode.svg surface once apply_styling() can size it for the display scale.
        self._mode_icon_names = {
            "apps": "view-app-grid-symbolic",
            "files": "folder-symbolic",
        }
        self.apps_dot = self._make_mode_dot(self._mode_icon_names["apps"], "Applications", "mode-dot-1")
        self.files_dot = self._make_mode_dot(self._mode_icon_names["files"], "Files", "mode-dot-2")
        self.apps_dot.connect("clicked", lambda *_: self.on_mode_dot_clicked("apps"))
        self.files_dot.connect("clicked", lambda *_: self.on_mode_dot_clicked("files"))
        for dot in (self.apps_dot, self.files_dot):
            self.mode_dots.pack_start(dot, False, False, 0)

        self.prompt.pack_start(self.prompt_input_overlay, True, True, 0)
        self.prompt.pack_end(self.mode_dots, False, False, 0)

        self.results_view = ResultsView(
            self.settings, self.apply_css, self.activate_result, self.on_results_visibility_changed
        )
        self.app_grid = AppGridView(on_launched=self.close)
        self.blank_page = Gtk.Box()

        # A single Stack owns which content is on screen (results / apps grid /
        # nothing), instead of each view separately calling its own .show()/
        # .hide() -- that let two views end up visible at once (e.g. clicking
        # Files while the apps grid was showing left both stacked). Stack also
        # gives a real cross-fade between pages for free instead of a snap.
        # vhomogeneous=False so the Stack sizes to whichever page is actually
        # showing, not the tallest of the three.
        self.content_stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=180,
            vhomogeneous=False,
        )
        self.content_stack.add_named(self.blank_page, "blank")
        self.content_stack.add_named(self.results_view, "results")
        self.content_stack.add_named(self.app_grid, "apps")
        self.content_stack.set_visible_child_name("blank")

        self.theme_root.pack_start(prompt_container, False, True, 0)
        self.theme_root.pack_start(self.content_stack, False, True, 0)

        self.frame.show_all()

        self.connect("focus-in-event", lambda *_: self.on_focus_in())
        self.connect("focus-out-event", lambda *_: self.on_focus_out())
        self.prompt_input.connect("changed", lambda *_: self.on_input_changed())
        self.prompt_input.connect("key-press-event", self.on_input_key_press)
        self.prompt_input.connect("focus-in-event", lambda *_: self.on_prompt_focus_in())
        self.connect("draw", self.on_initial_draw)
        self.prefs_btn.connect("clicked", lambda *_: self.get_app().show_preferences())

        # Try setting a transparent background
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is None:
            logger.info("Screen does not support alpha channels")
            visual = screen.get_system_visual()

        self.set_visual(visual)

        is_composited = screen.is_composited()
        logger.debug("Screen is composited: %s", is_composited)
        if not is_composited:
            # without a compositor deferred would lead to "flash of unstyled content"
            self.apply_styling()

        self.set_keep_above(True)
        self.present()
        # note: present_with_time is needed on some DEs to defeat focus stealing protection
        # (Gnome 3 forks like Cinnamon or Budgie, but not Gnome 3 itself any longer)
        # The correct time to use is the time of the user interaction requesting the focus, but we don't have access
        # to that, so we use `Gdk.CURRENT_TIME`, which is the same as passing 0.
        self.present_with_time(Gdk.CURRENT_TIME)
        super().show()

        if self.query_str:
            self.set_input(self.query_str)

    def _make_mode_dot(self, icon_name: str, tooltip: str, css_class: str) -> Gtk.Button:
        # Sized via CSS min-width/min-height (not width_request/height_request)
        # so dissolving can animate it down to 0 instead of an instant resize.
        btn = Gtk.Button(
            receives_default=False,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            tooltip_text=tooltip,
        )
        style = btn.get_style_context()
        style.add_class("mode-dot")
        style.add_class(css_class)
        btn.set_image(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.LARGE_TOOLBAR))
        return btn

    def on_mode_dot_clicked(self, mode: str) -> None:
        if mode == "apps":
            self.placeholder_label.set_text("Applications")
            self.app_grid.render(list_desktop_apps())
            self._switch_content("apps")
        elif mode == "files":
            self.placeholder_label.set_text("Files")
            # Switch immediately rather than waiting for the query callback --
            # otherwise the apps grid stayed the visible Stack page until
            # results arrived, which is what made Files look like it was
            # rendering "above" the still-showing apps grid.
            self._switch_content("results")
            # Query the file browser directly rather than set_input("~") --
            # that would show the raw "~" in the entry instead of the "Files"
            # placeholder. query_changed() only needs the string, not the
            # entry's own text, so the two can stay decoupled.
            self.get_app().query_changed("~")
        self._active_mode = mode
        self._set_prompt_icon(mode)
        self._suppress_focus_revert = True
        self.prompt_input.grab_focus_without_selecting()
        self._suppress_focus_revert = False

    def _set_prompt_icon(self, mode: str | None) -> None:
        """Swap the entry's primary icon between the default magnifying glass and the
        active mode's own icon (the same icon its mode-dot uses), so the search bar
        reads as "[icon] Applications" etc. alongside the placeholder_label text."""
        if mode is None:
            self.prompt_input.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "edit-find-symbolic")
            return
        if mode == "apps" and self._apps_icon_pixbuf is not None:
            self.prompt_input.set_icon_from_pixbuf(Gtk.EntryIconPosition.PRIMARY, self._apps_icon_pixbuf)
            return
        self.prompt_input.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, self._mode_icon_names[mode])

    def _revert_active_mode(self) -> None:
        """Undo an active mode's icon/name/content back to idle, ready for regular typing."""
        self._active_mode = None
        self.placeholder_label.set_text(self._default_placeholder)
        self._set_prompt_icon(None)
        self._switch_content("blank")
        self.get_app().query_changed("")

    def on_prompt_focus_in(self) -> None:
        """Clicking back into the search bar while a mode's results are showing (Applications
        grid, Files listing) but nothing has been typed yet -- treat it as "never mind, let me
        type a regular search" rather than leaving the stale icon/name/results up."""
        if self._suppress_focus_revert:
            return
        if self._active_mode and not self.prompt_input.get_text():
            self._revert_active_mode()

    def _switch_content(self, page: str) -> None:
        """Single place that decides what's on screen (blank / results /
        apps) -- the Stack cross-fades between pages, and only one page is
        ever visible at a time."""
        self.content_stack.set_visible_child_name(page)
        style = self.theme_root.get_style_context()
        if page == "blank":
            style.remove_class("has-content")
        else:
            style.add_class("has-content")

    def _dissolve_mode_dots(self) -> None:
        """Fade + scatter the dots out (dust-diminishing look) while the row's
        own min-width/margin collapse to 0 over the same transition, so the
        search entry grows into the freed space smoothly instead of snapping
        once the row is fully hidden."""
        if self._dissolve_timer:
            return  # already dissolving
        self.mode_dots.get_style_context().add_class("dissolving")
        for dot in (self.apps_dot, self.files_dot):
            dot.get_style_context().add_class("dissolving")
        # 360ms safely clears mode-dot-2's 40ms transition-delay + its 260ms transition (see peachos.css)
        self._dissolve_timer = scheduling.timer(0.36, self._finish_dissolve)

    def _finish_dissolve(self) -> None:
        self._dissolve_timer = None
        if self.prompt_input.get_text():  # still non-empty -- really hide it
            self.mode_dots.set_visible(False)

    def _materialize_mode_dots(self) -> None:
        if self._dissolve_timer:
            self._dissolve_timer.cancel()
            self._dissolve_timer = None
        self.mode_dots.set_visible(True)
        self.mode_dots.get_style_context().remove_class("dissolving")
        for dot in (self.apps_dot, self.files_dot):
            dot.get_style_context().remove_class("dissolving")

    def on_results_visibility_changed(self, has_results: bool) -> None:
        """Called by ResultsView whenever a search/Files query's own results
        go from empty to non-empty or back (see the on_visibility_changed
        callback passed into its constructor)."""
        self._switch_content("results" if has_results else "blank")

    def apply_styling(self) -> None:
        """
        Apply styling and position the window.

        Note that this method is slow and should be called after the window is shown if possible.
        """
        if self.get_opacity() == 1:  # already applied styling
            return

        self.theme_root.get_style_context().add_class("app")
        self.prompt_input.get_style_context().add_class("input")
        self.prefs_btn.get_style_context().add_class("prefs-btn")
        prefs_icon_surface = load_icon_surface(f"{paths.ASSETS}/icons/gear.svg", 16, self.get_scale_factor())
        self.prefs_btn.set_image(Gtk.Image.new_from_surface(prefs_icon_surface))

        # Real Applications icon for the apps mode-dot (22px to match ".mode-dot image" in
        # peachos.css), tinted to the same #2c5f7c that CSS rule uses for the symbolic
        # folder-symbolic/edit-paste-symbolic dot icons -- see _tint_icon_surface.
        apps_icon_path = f"{paths.ASSETS}/icons/applications-mode.svg"
        apps_dot_surface = load_icon_surface(apps_icon_path, 22, self.get_scale_factor())
        apps_dot_tinted = _tint_icon_surface(apps_dot_surface, (0x2C / 255, 0x5F / 255, 0x7C / 255, 1.0))
        self.apps_dot.set_image(Gtk.Image.new_from_surface(apps_dot_tinted))

        # Same treatment for the entry's primary icon, tinted to match the muted
        # rgba(60, 75, 90, 0.6) that ".input image" uses for the same two symbolic icons.
        # Entry's icon setter only takes a GdkPixbuf, not a cairo surface, hence the convert.
        entry_icon_surface = load_icon_surface(apps_icon_path, 18, self.get_scale_factor())
        entry_icon_tinted = _tint_icon_surface(entry_icon_surface, (60 / 255, 75 / 255, 90 / 255, 0.6))
        w, h = entry_icon_tinted.get_width(), entry_icon_tinted.get_height()
        self._apps_icon_pixbuf = Gdk.pixbuf_get_from_surface(entry_icon_tinted, 0, 0, w, h)

        self.apply_theme()
        self.position_window()
        self.set_opacity(1)

    def deferred_init(self) -> None:
        if not self.get_application():
            # Runs from an idle callback, so the window may already be closed.
            return
        if self.query_str:
            # select all text in the input field.
            # used when user turns off "start with blank query" setting
            self.prompt_input.select_region(0, -1)
        self.apply_styling()
        self.get_app().window_ready()

    ######################################
    # GTK Signal Handlers
    ######################################

    def on_initial_draw(self, *_: tuple[Any]) -> None:
        # ULAUNCHER_PERF_START_BOOTTIME is a perf-test probe (see `make perf` in makefile).
        # When set, report elapsed time from the caller's externally-captured /proc/uptime and
        # exit before deferred_init - this is the earliest point at which keyboard input registers.
        if t0 := os.environ.get("ULAUNCHER_PERF_START_BOOTTIME"):
            elapsed_ms = (time.clock_gettime(time.CLOCK_BOOTTIME) - float(t0)) * 1000
            sys.stdout.write(f"ULAUNCHER_PERF first_draw_ms={elapsed_ms:.2f}\n")
            sys.stdout.flush()
            if app := self.get_application():
                app.quit()
            return
        logger.info("Window shown")
        self.disconnect_by_func(self.on_initial_draw)
        scheduling.run_when_idle(self.deferred_init)

    def on_focus_out(self) -> None:
        if self.settings.close_on_focus_out:
            self.close(save_query=True)

    def on_focus_in(self) -> None:
        if self.settings.grab_mouse_pointer:
            self.toggle_grab_pointer_device(True)

    def on_input_changed(self) -> None:
        """
        Triggered by user input
        """
        query_str = self.prompt_input.get_text()
        self.placeholder_label.set_visible(not query_str)
        # Idle-state mode picker only makes sense with an empty query -- see on_mode_dot_clicked.
        if query_str:
            self.placeholder_label.set_text(self._default_placeholder)
            self._active_mode = None
            self._set_prompt_icon(None)
            self._dissolve_mode_dots()
            # Leave the apps grid immediately rather than waiting for the
            # query callback -- see the same note in on_mode_dot_clicked's
            # "files" branch.
            if self.content_stack.get_visible_child_name() == "apps":
                self._switch_content("blank")
        else:
            self._materialize_mode_dots()
        self.get_app().query_changed(query_str)

    def activate_result(self, alt: bool) -> None:
        """
        Activate the selected result
        """
        if result := self.results_view.get_active_result():
            self.get_app().activate_result(result, alt)

    def on_input_key_press(self, entry_widget: Gtk.Entry, event: Gdk.EventKey) -> bool:  # noqa: PLR0911
        """
        Triggered by user key press
        Return True to stop other handlers from being invoked for the event
        """
        keyname = Gdk.keyval_name(event.keyval)
        alt = bool(event.state & Gdk.ModifierType.MOD1_MASK)
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        jump_keys = self.settings.get_jump_keys()

        use_arrow_key_aliases = len(self.settings.arrow_key_aliases) == 4  # noqa: PLR2004
        arrow_key_aliases = [*self.settings.arrow_key_aliases] if use_arrow_key_aliases else [None] * 4
        left_alias, down_alias, up_alias, right_alias = arrow_key_aliases
        if not use_arrow_key_aliases:
            logger.warning(
                "Invalid value for arrow_key_aliases: %s, expected four letters", self.settings.arrow_key_aliases
            )

        if keyname == "Escape":
            self.close(save_query=True)
            return True

        if ctrl and keyname == "comma":
            self.get_app().show_preferences()
            return True

        if (
            keyname == "BackSpace"
            and not ctrl
            and not entry_widget.get_selection_bounds()
            and entry_widget.get_position() == len(self.query_str)
            and self.get_app().handle_backspace(self.query_str)
        ):
            return True

        if self.results_view.has_results:
            if keyname in ("Up", "ISO_Left_Tab") or (ctrl and keyname == up_alias):
                self.results_view.go_up()
                return True

            if keyname in ("Down", "Tab") or (ctrl and keyname == down_alias):
                self.results_view.go_down()
                return True

            if ctrl and keyname == left_alias:
                entry_widget.set_position(max(0, entry_widget.get_position() - 1))
                return True

            if ctrl and keyname == right_alias:
                entry_widget.set_position(entry_widget.get_position() + 1)
                return True

            if keyname in ("Return", "KP_Enter"):
                self.activate_result(alt)
                return True
            if alt and event.string in jump_keys:
                self.results_view.select(jump_keys.index(event.string))
                return True
        return False

    ######################################
    # Helpers
    ######################################

    def get_app(self) -> UlauncherApp:
        return cast("UlauncherApp", self.get_application())

    @property
    def query_str(self) -> str:
        return self.get_app().query

    def apply_css(self, widget: Gtk.Widget) -> None:
        if not self._css_provider:
            self._css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider(
            widget.get_style_context(), self._css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        if isinstance(widget, Gtk.Container):
            widget.forall(self.apply_css)

    def _get_shadow_size(self) -> int:
        if not self.is_composited():
            return 0

        return self.settings.window_shadow

    def apply_theme(self) -> None:
        if not self._css_provider:
            self._css_provider = Gtk.CssProvider()
        # Load theme CSS and apply shadow
        theme_css = Theme.load(self.settings.theme_name).get_css(self._get_shadow_size())
        self._css_provider.load_from_data(theme_css.encode())
        self.apply_css(self)
        logger.info('Applying theme "%s"', self.settings.theme_name)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)

    def get_layout_size(self) -> Gdk.Rectangle | None:
        """The monitor size to lay the window out for"""
        # Mutter decides which monitor the window opens on and Wayland doesn't tell us which one
        # it picked, so use the smallest monitor to ensure the window fits on all of them. A window
        # taller than the monitor gets its top edge clamped to the work area, which would shift the
        # content down relative to the monitor.
        if DESKTOP_ID == "GNOME" and not IS_X11_COMPATIBLE:
            if not (geometries := get_monitor_geometries()):
                return None
            layout_size = Gdk.Rectangle()
            layout_size.width = min(geometry.width for geometry in geometries)
            layout_size.height = min(geometry.height for geometry in geometries)
            return layout_size

        if monitor := get_monitor(self.settings.render_on_screen != "default-monitor"):
            return monitor.get_geometry()
        return None

    def position_window(self) -> None:
        if layout_size := self.get_layout_size():
            window_width = self.settings.base_width
            pos_x = (layout_size.width - window_width) / 2
            pos_y = layout_size.height * 0.1

            prompt_height = self.prompt.get_allocated_height()
            # -ROW_HEIGHT_PX: was tall enough to overlap the dock at the
            # bottom of the screen -- trim it back by one app-grid row.
            max_height = layout_size.height - prompt_height - pos_y * 2 - ROW_HEIGHT_PX
            self.results_view.set_max_height(int(max_height))
            self.app_grid.set_max_height(int(max_height))

            # Part II of the Gnome Wayland fix (see above in __init__)
            # Use margins to center the visible content within the full-screen window
            if DESKTOP_ID == "GNOME" and not IS_X11_COMPATIBLE:
                self.frame.set_properties(
                    margin_top=pos_y,
                    margin_bottom=pos_y,
                    margin_start=pos_x,
                    margin_end=pos_x,
                )

            elif self.layer_shell_enabled:
                layer_shell.set_vertical_position(self, pos_y)
            else:
                self.move(int(pos_x + layout_size.x), int(pos_y + layout_size.y))

    def close(self, save_query: bool = False) -> None:
        logger.info("Closing Ulauncher window")
        if not save_query or not self.settings.auto_resume:
            self.get_app().set_query("", update_input=False)
        if self.settings.grab_mouse_pointer:
            self.toggle_grab_pointer_device(False)
        super().close()
        self.destroy()

    def toggle_grab_pointer_device(self, grab: bool) -> None:
        if window := self.get_window():
            seat = window.get_display().get_default_seat()
            if not window or not seat:
                logger.warning("Could not get the pointer device.")
                return

            if not grab:
                seat.ungrab()
                return

            grab_status = seat.grab(window, Gdk.SeatCapabilities.ALL_POINTING, True)
            logger.debug("Focus in event, grabbing pointer: %s", grab_status)

    def set_input(self, query_str: str) -> None:
        self.prompt_input.set_text(query_str)
        self.prompt_input.set_position(-1)

    def show_results(self, update: ResultsUpdate) -> None:
        self.results_view.render(update)
