from __future__ import annotations

import logging
import os
import sys
import time
from typing import TYPE_CHECKING, Any, cast

from gi.repository import Gdk, Gtk

from ulauncher import paths
from ulauncher.internals.results_update import ResultsUpdate
from ulauncher.ui.app_grid_view import AppGridView, list_desktop_apps
from ulauncher.ui.helpers import layer_shell
from ulauncher.ui.helpers.monitor import get_monitor, get_monitor_geometries
from ulauncher.ui.helpers.theme import Theme
from ulauncher.ui.load_icon_surface import load_icon_surface
from ulauncher.ui.results_view import ResultsView
from ulauncher.utils import scheduling
from ulauncher.utils.environment import DESKTOP_ID, IS_X11_COMPATIBLE
from ulauncher.utils.settings import Settings

if TYPE_CHECKING:
    from ulauncher.ui.app import UlauncherApp

logger = logging.getLogger(__name__)


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
            icon_name="ulauncher",
            opacity=0,  # set to 0 so we can show the window and get keyboard input before it's fully loaded
            resizable=False,
            skip_pager_hint=True,
            skip_taskbar_hint=True,
            title="Ulauncher - Application Launcher",
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
        # "Applications"/"Files"/"Clipboard". A bare Label has no GdkWindow, so
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

        # Idle-state mode picker (Applications / Files / Clipboard), macOS Spotlight style.
        # Dissolves as soon as there's a query -- see on_input_changed.
        self._dissolve_timer: scheduling.Context | None = None
        self.mode_dots = Gtk.Box(spacing=10, valign=Gtk.Align.CENTER)
        self.mode_dots.get_style_context().add_class("mode-dots-row")
        self.apps_dot = self._make_mode_dot("view-app-grid-symbolic", "Applications", "mode-dot-1")
        self.files_dot = self._make_mode_dot("folder-symbolic", "Files", "mode-dot-2")
        self.clipboard_dot = self._make_mode_dot("edit-paste-symbolic", "Clipboard (coming soon)", "mode-dot-3")
        self.apps_dot.connect("clicked", lambda *_: self.on_mode_dot_clicked("apps"))
        self.files_dot.connect("clicked", lambda *_: self.on_mode_dot_clicked("files"))
        self.clipboard_dot.connect("clicked", lambda *_: self.on_mode_dot_clicked("clipboard"))
        for dot in (self.apps_dot, self.files_dot, self.clipboard_dot):
            self.mode_dots.pack_start(dot, False, False, 0)

        self.prompt.pack_start(self.prompt_input_overlay, True, True, 0)
        self.prompt.pack_end(self.mode_dots, False, False, 0)

        self.results_view = ResultsView(
            self.settings, self.apply_css, self.activate_result, self.on_results_visibility_changed
        )
        self.app_grid = AppGridView(on_launched=self.close)

        self.theme_root.pack_start(prompt_container, False, True, 0)
        self.theme_root.pack_start(self.results_view, False, True, 0)
        self.theme_root.pack_start(self.app_grid, False, True, 0)

        self.frame.show_all()

        self.connect("focus-in-event", lambda *_: self.on_focus_in())
        self.connect("focus-out-event", lambda *_: self.on_focus_out())
        self.prompt_input.connect("changed", lambda *_: self.on_input_changed())
        self.prompt_input.connect("key-press-event", self.on_input_key_press)
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
            self._show_app_grid()
        elif mode == "files":
            self.placeholder_label.set_text("Files")
            # Query the file browser directly rather than set_input("~") --
            # that would show the raw "~" in the entry instead of the "Files"
            # placeholder. query_changed() only needs the string, not the
            # entry's own text, so the two can stay decoupled.
            self.get_app().query_changed("~")
        elif mode == "clipboard":
            self.placeholder_label.set_text("Clipboard")  # Not built yet -- deferred.
        self.prompt_input.grab_focus_without_selecting()

    def _show_app_grid(self) -> None:
        self.app_grid.render(list_desktop_apps())
        self.results_view.hide()
        self.app_grid.show()
        self.on_results_visibility_changed(True)

    def _hide_app_grid(self) -> None:
        if self.app_grid.get_visible():
            self.app_grid.hide()

    def _dissolve_mode_dots(self) -> None:
        """Fade + scatter the dots out (dust-diminishing look) while the row's
        own min-width/margin collapse to 0 over the same transition, so the
        search entry grows into the freed space smoothly instead of snapping
        once the row is fully hidden."""
        if self._dissolve_timer:
            return  # already dissolving
        self.mode_dots.get_style_context().add_class("dissolving")
        for dot in (self.apps_dot, self.files_dot, self.clipboard_dot):
            dot.get_style_context().add_class("dissolving")
        # 340ms = mode-dot-3's 80ms transition-delay + its 260ms transition (see peachos.css)
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
        for dot in (self.apps_dot, self.files_dot, self.clipboard_dot):
            dot.get_style_context().remove_class("dissolving")

    def on_results_visibility_changed(self, has_results: bool) -> None:
        """Liquid-glass panel behind the results/category content -- stays off
        in the idle state so it's just the floating pill + dots."""
        style = self.theme_root.get_style_context()
        if has_results:
            style.add_class("has-content")
        else:
            style.remove_class("has-content")

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
            self._dissolve_mode_dots()
            self._hide_app_grid()
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
            max_height = layout_size.height - prompt_height - pos_y * 2
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
