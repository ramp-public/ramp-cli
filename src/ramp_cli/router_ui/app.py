"""A persistent, keyboard-first Router app. No history of prior UI frames."""

import asyncio
import os
import re
import time
from collections.abc import Callable
from threading import Event
from uuid import uuid4

import click
import httpx
from textual import events
from textual.app import App
from textual.binding import Binding
from textual.theme import BUILTIN_THEMES, Theme
from textual.widget import Widget
from textual.widgets import (
    DataTable,
    Static,
)

from ramp_cli.errors import ApiError, RampCLIError
from ramp_cli.router_ui.animation import (
    BrandAnimation,
)
from ramp_cli.router_ui.dialogs import (
    ChoiceDialog,
    Confirm,
    Notice,
)
from ramp_cli.router_ui.pages import (
    CellEditing,
    CollectionPage,
    FormPage,
    ResultPage,
    RouterPage,
)
from ramp_cli.router_ui.palette import (
    MODES,
    Palette,
    environment_is_light,
    make_palette,
)
from ramp_cli.router_ui.service import (
    RouterService,
    safe_text,
)
from ramp_cli.router_ui.tabs.harnesses import (
    ConnectPage,
    DisconnectPage,
    HarnessesPage,
    HarnessModelPage,
    HarnessRoutingPage,
    LegacyPage,
)
from ramp_cli.router_ui.tabs.home import (
    AccountPage,
    HomePage,
    _latency,
    _tokens,
    usage_latency,
)
from ramp_cli.router_ui.tabs.keys import (
    CreateKeyPage,
    KeyEditPage,
    KeyPage,
    KeysPage,
    KeyTable,
    SecretPage,
    harness_labels,
    parse_key_name,
)
from ramp_cli.router_ui.tabs.strategies import (
    StrategiesPage,
    StrategyTable,
    inherited_label,
    key_count,
    parse_strategy_name,
)
from ramp_cli.router_ui.terminal import (
    QUERY_COLORS,
    TerminalColor,
    install_color_detection,
)
from ramp_cli.router_ui.widgets import (
    ARROW_BINDINGS,
    CHART_GROW_SECONDS,
    MIN_CHART_ROWS,
    PAGES,
    REFRESHING,
    TAB_PAGES,
    WIDE_METRICS,
    ArrowNavigation,
    Button,
    CellChecklist,
    CellInput,
    CellMenu,
    CellTable,
    InlineModal,
    KeyHints,
    Marquee,
    MetricDivider,
    PaletteText,
    RouterFooter,
    RouterTable,
    RouterTabs,
    arrow_owned,
    arrow_row,
    button_styles,
    centred_width,
    focus_first_action,
    parse_spend_cap,
    scroll_area,
    title_case,
)


class RouterApp(App):
    """One alternate-screen session; writes cannot be abandoned halfway through."""

    TITLE = "Router"
    ENABLE_COMMAND_PALETTE = False
    # Blank lines between the shell prompt and the tab bar in --inline mode.
    INLINE_PADDING = 0
    BINDINGS = [
        Binding("ctrl+q", "quit_router", "Quit", priority=True),
        Binding("ctrl+c", "quit_router", "Quit", show=False, priority=True),
        Binding("ctrl+t", "toggle_motion", "Motion", priority=True),
    ]
    CSS = """
    Screen { background: $router-background; color: $router-foreground; }
    #topbar { height: 2; margin: 0 1; }
    #identity { width: auto; height: 2; padding: 0 0 0 2; color: $router-muted; border-bottom: heavy $foreground 10%; }
    #identity.focused { border-bottom: heavy $foreground 30%; }
    #navigation { width: 1fr; height: 2; background: $router-background; }
    #navigation Tab { color: $router-muted; background: $router-background; }
    #navigation Tab.-active { color: $router-accent; text-style: bold; }
    #navigation:focus Tab.-active { color: $router-active-accent; background: $router-active; text-style: bold; }
    #frame { width: 100%; margin: 0 1; border-bottom: ledge $router-frame-border; }
    #body { width: 100%; padding: 0 0 0 1; }
    #body.table-body { overflow: hidden; }
    #status-row { width: 100%; height: auto; margin: 1 0 1 2; }
    #status-row > .heading { width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #status-row.has-heading #status, #status-row.has-status > .heading { display: none; }
    #status-row.has-heading.has-status #status { display: block; }
    #status { width: 1fr; height: auto; max-height: 5; min-height: 1; color: $router-muted; }
    #keys { width: auto; height: 1; padding: 0 0 0 2; color: $router-muted; }
    #keys > .keys--key { color: $router-foreground 65%; }
    #status.error { color: $router-error; }
    #claim { display: none; width: 15; margin: 0 0 0 2; }
    #status-row.has-claim #claim { display: block; }
    #status-row.has-claim #keys { display: none; }
    #status-row.has-claim #status { height: 3; content-align: left middle; }
    #body > #secret-warning { margin: 1 0 1 1; color: $router-accent; text-style: bold; }
    #activity { display: none; width: 100%; height: 1fr; content-align: center middle; text-align: center; color: $router-accent; }
    RouterPage.loading #activity { display: block; }
    Footer { background: $router-background; color: $router-muted; }
    Footer > .footer--key { background: $router-active; color: $router-active-foreground; }
    #footer-version { dock: right; width: auto; height: 1; padding: 0 1; color: $router-muted; background: $router-background; }
    .heading { text-style: bold; color: $router-accent; }
    #body > Static, #body > Label { margin-left: 1; }
    HarnessesPage #body > .heading { margin: 0 0 1 1; }
    .muted { color: $router-muted; }
    Static { height: auto; }
    Label { margin-top: 1; height: auto; color: $router-muted; }
    Input { margin-bottom: 1; background: $router-background; background-tint: transparent; border: solid $router-control-border; padding: 0 1; }
    Input:focus { border: solid $router-accent; background-tint: transparent; }
    Select { height: 3; margin-bottom: 1; background: $router-background; background-tint: transparent; border: none; padding: 0; }
    Select > SelectCurrent { height: 3; background: $router-background; background-tint: transparent; border: solid $router-control-border; padding: 0 1; }
    Select:focus > SelectCurrent, Select.-expanded > SelectCurrent { border: solid $router-accent; background-tint: transparent; }
    SelectOverlay { background: $router-background; background-tint: transparent; border: solid $router-control-border; }
    Button { background: $router-background; color: $router-foreground; border: none; margin-right: 1; height: 3; min-height: 3; padding: 0; content-align: center middle; }
    Button:hover { background: $router-background; }
    Button.-primary { background: $router-background; color: $router-foreground; border: none; }
    Button:focus, Button.-primary:focus { background: $router-accent; border: none; text-style: bold; background-tint: transparent; }
    .actions { height: 3; min-height: 3; padding: 0; margin: 0; }
    /* The frame's ▔ bottom edge already leaves most of its row blank, so
       only one row below matches the gap above. */
    #page-actions { margin: 0 1 1 1; }
    #page-actions:empty { display: none; }
    HarnessesPage #page-actions Button { width: 18; }
    /* Room for Change Defaults with three columns either side; Create keeps the
       default width the other tabs use. */
    StrategiesPage #page-actions #legacy { width: 23; }
    ConnectPage { align: center middle; background: $router-overlay; }
    #connect-dialog { width: 76; max-width: 90%; height: auto; max-height: 90%; padding: 1 2; border: solid $router-frame-border; background: $router-background; }
    /* Docked, so a tall form scrolls while the buttons stay in view. */
    #connect-title { dock: top; text-style: bold; margin-bottom: 1; }
    #connect-footer { dock: bottom; height: auto; }
    #connect-status { margin: 0 0 1 0; color: $router-muted; }
    #connect-body { height: auto; scrollbar-size-vertical: 1; }
    #connect-body Label { margin-top: 0; }
    #connect-body Input, #connect-body Select { margin-bottom: 1; }
    /* Odd labels get odd widths so their spare cells split evenly. */
    #connect-actions Button { width: 18; margin: 0; }
    #connect-actions #save { width: 19; }
    #connect-back { margin-right: 1; }
    #connect-actions > .spacer { width: 1fr; }
    #new-key, #advanced-options { height: auto; }
    #advanced-options { display: none; }
    #advanced-options.open { display: block; }
    DataTable { height: 1fr; min-height: 3; background: $router-background; margin-bottom: 1; }
    DataTable > .datatable--header { background: $router-active; color: $router-active-foreground; text-style: bold; }
    DataTable > .datatable--cursor { background: $router-active; color: $router-active-accent; }
    DataTable > .datatable--hover { background: $router-hover; color: $router-hover-foreground; }
    Checkbox { background: $router-background; border: none; padding: 0; height: 3; }
    Checkbox > .toggle--button { color: $router-surface; background: $router-surface; }
    Checkbox.-on > .toggle--button { color: $router-surface-accent; background: $router-surface; }
    SelectionList { height: auto; min-height: 3; overflow-y: hidden; background: $router-surface; color: $router-surface-foreground; border: solid $router-button-border; }
    SelectionList:focus { border: solid $router-accent; }
    SelectionList > .option-list--option-highlighted { background: $router-active; color: $router-active-accent; }
    Confirm, Notice { align: center middle; background: $router-overlay; }
    #confirmation { width: 64; max-width: 90%; height: auto; padding: 1 2; border: solid $router-frame-border; background: $router-background; }
    #confirmation > Static { margin-bottom: 1; }
    #confirmation Button { width: 1fr; }
    #confirmation.notice { width: 76; }
    #confirmation.notice Button { width: 18; }
    #confirmation.notice .spacer { width: 1fr; }
    #confirmation.choices { width: 76; }
    #choice-list { height: auto; max-height: 10; padding: 0; background: $router-background; color: $router-foreground; border: solid $router-frame-border; }
    #choice-list:focus { border: solid $router-accent; }
    #choice-list > .option-list--option-highlighted { background: $router-active; color: $router-active-accent; text-style: bold; }
    CellMenu, CellInput { background: transparent; }
    #cell-input { height: auto; padding: 0 1; background: $router-background; border: solid $router-accent; }
    #cell-input Input { margin: 0; }
    #cell-input #cell-error { color: $router-error; }
    #cell-input Button { width: 1fr; min-width: 0; }
    #cell-input #cell-set { margin-right: 0; }
    #cell-menu { height: auto; padding: 0; background: $router-background; color: $router-foreground; border: solid $router-accent; }
    #cell-menu-box { height: auto; background: $router-background; border: solid $router-accent; }
    #cell-menu-box > #cell-menu { border: none; }
    #cell-menu-header { height: 1; color: $router-muted; text-style: bold; }
    #cell-menu > .option-list--option-highlighted { background: $router-active; color: $router-active-accent; text-style: bold; }
    #cell-checklist { height: auto; padding: 0; background: $router-background; color: $router-foreground; border: solid $router-accent; border-subtitle-color: $router-muted; }
    #cell-checklist > .option-list--option-highlighted { background: $router-active; color: $router-active-accent; text-style: bold; }
    AccountPage #status, HomePage #status, AccountPage #keys { display: none; }
    HomePage #keys { display: none; }
    AccountPage #status-row { margin: 0 2; }
    HomePage #status-row { margin: 0 2; }
    AccountPage #body, HomePage #body { padding: 0; }
    #home-content { width: 100%; height: 100%; align: center top; opacity: 0%; }
    #home-content.fitted { opacity: 100%; }
    #home-hero { width: 100%; height: 1fr; align: center middle; margin: 0; }
    #home-hero BrandAnimation { width: 100%; margin: 0; content-align: center middle; }
    #home-tagline { width: 100%; height: 1; margin: 0 0 1 0; text-align: center; color: $router-muted; }
    #home-notice { display: none; width: 100%; height: 1; margin: 0 0 1 0; text-align: center; color: $router-foreground; }
    #home-signin { display: none; width: 100%; height: 3; margin: 0 0 1 0; align: center top; }
    #home-signin Button { width: 18; margin: 0; }
    #home-content.signed-out #home-tagline { display: none; }
    #home-content.signing-in #home-tagline { display: none; }
    #home-content.signing-in.squeezed #home-summary-wrap { display: none; }
    #home-content.signed-out #home-notice, #home-content.signed-out #home-signin { display: block; opacity: 0%; }
    /* Transparent until lifted under the wordmark, so they never flash low. */
    #home-content.signed-out #home-notice.placed, #home-content.signed-out #home-signin.placed { opacity: 100%; }
    AccountPage.hero-loading #home-content #home-notice, AccountPage.hero-loading #home-content #home-signin,
    AccountPage.refreshing #home-content #home-notice, AccountPage.refreshing #home-content #home-signin { display: none; }
    #home-activity { display: none; width: 100%; height: 1; margin: 0 0 1 0; text-align: center; color: $router-accent; }
    AccountPage.refreshing #home-tagline { display: none; }
    AccountPage.refreshing #home-activity { display: block; margin: 1 0; }
    AccountPage.hero-loading #home-tagline { display: none; }
    AccountPage.hero-loading #home-activity { display: block; opacity: 0%; }
    AccountPage.hero-loading #home-activity.placed { opacity: 100%; }
    #home-usage { width: 100%; height: 1fr; display: none; padding: 0 0 0 2; }
    #home-content.signed-in #home-usage { display: block; }
    #home-content.signed-in #home-hero, #home-content.signed-in #home-tagline { display: none; }
    #usage-header { width: 100%; height: 3; margin-top: 0; }
    #usage-legend { width: 1fr; height: 1; margin-top: 1; }
    #usage-days { width: 15; height: 3; margin: 0; }
    #usage-days > SelectCurrent { height: 3; padding: 0 1; border: solid $router-control-border; }
    #usage-days:focus > SelectCurrent, #usage-days.-expanded > SelectCurrent { border: solid $router-accent; }
    #usage-empty { width: 100%; margin: 1 0; content-align: center middle; }
    /* Drawn to fit; below MIN_CHART_ROWS the whole chart section hides. */
    #usage-scroll { width: 1fr; height: 1fr; overflow: hidden hidden; margin: 1 2 1 0; }
    #home-content.no-chart #home-usage { display: none; }
    #account-summary { height: 9; width: 100%; margin: 0; }
    /* The row under the cells matches the gap above them (#usage-scroll). */
    #home-summary-wrap { width: 100%; height: 9; align: center top; margin-bottom: 1; }
    .metric-row { width: 100%; height: 4; }
    .account-metric { width: 1fr; height: 4; padding: 1 3 0 3; background: $router-surface; border-right: solid $router-background; }
    .account-metric.row-end { border-right: none; }
    .account-metric Static { width: 100%; text-align: left; }
    #metric-divider { width: 100%; height: 1; background: $router-surface; color: $router-background; }
    #home-content.compact #account-summary, #home-content.compact #home-summary-wrap { height: 7; }
    #home-content.compact .metric-row { height: 3; }
    #home-content.compact .account-metric { height: 3; padding: 0 2; }
    /* Without the chart the cells fill its space, their text centered in each. */
    #home-content.signed-in.no-chart #home-summary-wrap, #home-content.signed-in.no-chart #account-summary { height: 1fr; }
    #home-content.signed-in.no-chart .metric-row { height: 1fr; }
    /* Full padding values: Textual's padding-top etc. reset the other sides. */
    #home-content.signed-in.no-chart .account-metric { height: 100%; padding: 0 3; align-vertical: middle; }
    #home-content.signed-in.no-chart.compact .account-metric { padding: 0 2; }
    #home-content.signed-in.no-chart #account-details { height: auto; }
    .metric-label { color: $router-muted; }
    #account-details { color: $router-foreground; height: 2; text-style: bold; }
    #account-credits { color: $router-accent; text-style: bold; }
    #today-spent { color: $router-foreground; text-style: bold; }
    #today-saved { color: $router-accent; text-style: bold; }
    #window-tokens, #window-latency { color: $router-foreground; text-style: bold; }
    #login-progress, #cancel-login, #callback-url, #copy-login-url { display: none; }
    #callback-url { margin: 0 0 1 0; }
    #page-actions > .spacer { width: 1fr; }
    #page-actions #settings { margin-right: 0; }
    #key-summary { margin-bottom: 1; }
    #key-usage { margin-top: 1; }
    #usage-models { height: auto; overflow-y: hidden; }
    AccountPage #status.visible { display: block; }
    RouterPage.loading #status-row, RouterPage.loading #body { display: none !important; }
    RouterPage.loading #page-actions { visibility: hidden; }
    """

    def __init__(
        self,
        service: RouterService,
        *,
        page: str = "home",
        options: dict | None = None,
        inline_height: int | None = None,
        theme_mode: str | None = None,
    ):
        super().__init__()
        self.theme_mode = (
            (theme_mode or os.environ.get("RAMP_ROUTER_THEME") or "auto")
            .strip()
            .lower()
        )
        if self.theme_mode not in MODES:
            raise click.BadParameter(
                "Choose auto, dark, light, or terminal.", param_hint="--theme"
            )
        self._light_hint = environment_is_light(os.environ.get("COLORFGBG"))
        self._terminal_background = None
        self._terminal_foreground = None
        self._theme_probe_enabled = False
        self._last_theme_probe = 0.0
        self._pending_colors: set[int] = set()
        self._color_timer = None
        self.palette = make_palette(self.theme_mode, light_hint=self._light_hint)
        self._register_palette(self.palette)
        self.theme = self._palette_theme_name
        self.service = service
        self.inline_height = inline_height
        self.initial_page = page
        self.initial_options = options or {}
        self.identity = "Router account"
        # One live screen per top-level tab, kept installed so revisits show
        # at once instead of composing and restyling a whole new page.
        self.tab_screens: dict[str, RouterPage] = {}
        # Set while a page loads (see load_page): reads freeze controls only
        # once slow, and quiet reads keep stale content in view meanwhile.
        self.reading = False
        self.quiet_load = False
        # Reads in flight. They run beside each other and never set busy,
        # so leaving a page never waits on its refresh.
        self.loads = 0
        self.busy = False
        self.home_loaded = False
        self.cancellable = False
        self.cancel_event = Event()
        # The control an operation is giving focus back to as it completes.
        self.restoring_focus: Widget | None = None
        # A change run_and_reload is folding into the page's next read.
        self.pending_write: tuple[str, Callable, str] | None = None
        self.started = 0.0
        self.operation_label = ""
        self.reduced_motion = os.environ.get("RAMP_ROUTER_REDUCED_MOTION") == "1"
        # The Router account on screen; data read for another is discarded.
        self.account = self.account_identity()
        # Bumped by every change: a read that started before one is stale, so
        # its result never replaces what the change's own reload shows.
        self.generation = 0
        # Controls an in-flight background read disabled, by page, so a newer
        # operation on that page can give them back before it starts.
        self.frozen: dict[int, list[tuple[Widget, bool]]] = {}

    def on_mount(self):
        self.theme = self._palette_theme_name
        if self.inline_height is not None:
            # Even the covered default screen participates in inline sizing.
            self.screen.styles.height = self.inline_height
        if self.initial_page in TAB_PAGES and not self.initial_options:
            self.push_screen(self.tab_page(self.initial_page)[0])
        else:
            self.push_screen(self.make_page(self.initial_page, self.initial_options))
        self.initial_options.clear()
        # The spinner ticks only while an operation is busy (see `busy`).
        self._activity_timer = self.set_interval(
            0.1, self.animate, pause=not self.active
        )
        self.query_terminal_colors()

    # Bumped on every app-wide restyle; see style_token.
    style_generation = 0

    def refresh_css(self, animate: bool = True):
        # Every theme swap (including terminal colour changes, which register
        # a new theme), ANSI theme change, and CSS variable change lands here.
        self.style_generation += 1
        super().refresh_css(animate)

    def style_token(self) -> tuple:
        """Everything app-wide that a screen's computed styles depend on.

        A cached tab compares this on resume to skip Textual's full restyle
        (see RouterPage._on_screen_resume). Besides refresh_css, it covers
        a reloaded or extended stylesheet and app classes and pseudo-classes
        (theme, dark/light, :focus/:blur, ANSI), which Textual only restyles
        on the current screen.
        """
        sheet = self.stylesheet
        return (
            self.style_generation,
            id(sheet),
            len(sheet.source),
            self.classes,
            self.pseudo_classes,
        )

    def _register_palette(self, palette: Palette):
        self._palette_theme_name = (
            "router-"
            + ("terminal-" if palette.native else "")
            + ("dark" if palette.dark else "light")
        )
        if self.theme_mode == "auto":
            self._palette_theme_name += (
                "-"
                + palette.background.removeprefix("#")
                + "-"
                + palette.foreground.removeprefix("#")
            )
        variables = palette.css_variables()
        if palette.native:
            variables = {
                **BUILTIN_THEMES[
                    "ansi-dark" if palette.dark else "ansi-light"
                ].variables,
                **variables,
                "ansi-background": "ansi_default",
                "ansi-foreground": "ansi_default",
            }
        self.register_theme(
            Theme(
                name=self._palette_theme_name,
                primary=palette.accent,
                secondary=palette.muted,
                accent=palette.accent,
                background=None if palette.native else palette.background,
                foreground=None if palette.native else palette.foreground,
                surface=palette.surface,
                panel=palette.surface,
                success=palette.success,
                warning=palette.accent,
                error=palette.error,
                dark=palette.dark,
                ansi=palette.native,
                variables=variables,
            )
        )

    def on_resize(self, event: events.Resize):
        if isinstance(self.screen, RouterPage) and self.screen.is_mounted:
            self.screen.fit_max_height()

    def _build_driver(self, headless, inline, mouse, size):
        driver = super()._build_driver(headless, inline, mouse, size)
        if self.theme_mode in ("auto", "terminal") and not headless:
            self._theme_probe_enabled = install_color_detection(driver)
        return driver

    def query_terminal_colors(self):
        if self._theme_probe_enabled and time.monotonic() - self._last_theme_probe >= 2:
            self._last_theme_probe = time.monotonic()
            self._pending_colors.clear()
            self._driver.write(QUERY_COLORS)
            self._driver.flush()

    def on_app_focus(self):
        if self.is_mounted:
            self.query_terminal_colors()

    def watch_app_focus(self, focused: bool):
        # Pause every animation timer while the terminal is unfocused. Watching
        # the reactive also catches the key or click that refocuses without an
        # AppFocus event. Terminals that never report focus stay focused.
        self.sync_activity()
        for screen in self.screen_stack:
            for widget in screen.query("BrandAnimation, Marquee"):
                widget.sync()

    def on_terminal_color(self, message: TerminalColor):
        if self.theme_mode not in ("auto", "terminal") or not re.fullmatch(
            r"#[0-9a-fA-F]{6}", message.color
        ):
            return
        if message.code == 11:
            self._terminal_background = message.color
        elif message.code == 10:
            self._terminal_foreground = message.color
        else:
            return
        # The background and foreground replies arrive separately; restyling
        # costs a full theme swap, so apply once both are in, or shortly after
        # a lone reply when the terminal answers only one query.
        self._pending_colors.add(message.code)
        if self._color_timer is not None:
            self._color_timer.stop()
            self._color_timer = None
        if self._pending_colors >= {10, 11}:
            self.apply_terminal_colors()
        else:
            self._color_timer = self.set_timer(0.05, self.apply_terminal_colors)

    def apply_terminal_colors(self):
        self._pending_colors.clear()
        if self._color_timer is not None:
            self._color_timer.stop()
            self._color_timer = None
        palette = make_palette(
            self.theme_mode,
            background=self._terminal_background,
            foreground=self._terminal_foreground,
            light_hint=self._light_hint,
        )
        if palette == self.palette:
            return
        self.palette = palette
        self._register_palette(palette)
        self.theme = self._palette_theme_name
        for screen in self.screen_stack:
            for table in screen.query(DataTable):
                for row in table.rows:
                    for column in table.columns:
                        value = table.get_cell(row, column)
                        if isinstance(value, PaletteText):
                            value.style = palette.rich(value.role)
                            table.update_cell(row, column, value)
            for animation in screen.query(BrandAnimation):
                animation.draw()
        # Cached tabs keep their drawn content while away, so they redraw too.
        for screen in {*self.screen_stack, *self.tab_screens.values()}:
            if isinstance(screen, AccountPage):
                screen.palette_changed()
        self.refresh()

    def make_page(self, name: str, options: dict | None = None) -> RouterPage:
        options = options or {}
        if name == "connect":
            return HarnessesPage(connect=dict(options))
        if name == "disconnect":
            return DisconnectPage(**options)
        if name == "key":
            return KeyPage(**options)
        if name == "create-key":
            return CreateKeyPage(**options)
        if name == "profile":
            # The strategy CLI commands open on the Strategies table itself.
            profile_id = options.get("profile_id")
            if options.get("delete_on_load"):
                intent = "delete"
            else:
                intent = "edit" if profile_id else "create"
            return StrategiesPage(target=profile_id, intent=intent)
        if name == "legacy":
            return LegacyPage(api_key=options.get("api_key"))
        if name == "harness-routing":
            return HarnessRoutingPage(options["client"])
        if name == "account":
            return AccountPage(**options)
        if name == "keys":
            return KeysPage(**options)
        return {
            "home": HomePage,
            "account": AccountPage,
            "harnesses": HarnessesPage,
            "keys": KeysPage,
            "strategies": StrategiesPage,
        }[name]()

    def update_identity(self, session: dict):
        user = session.get("user") or {}
        previous = self.identity
        if session.get("authenticated"):
            self.identity = safe_text(
                user.get("email") or user.get("id") or "Signed in"
            )
        else:
            self.identity = "Signed out · Home → Sign in"
        if self.identity == previous:
            return
        for screen in {*self.screen_stack, *self.tab_screens.values()}:
            if isinstance(screen, RouterPage) and screen.is_mounted:
                screen.show_identity(self.identity)
                # Another account's data must not show while it revalidates:
                # a cached tab hides its content on its next load instead.
                if screen is not self.screen:
                    screen.content_shown = False

    def open(self, page: str):
        if self.busy:
            self.notice_busy()
            return
        if isinstance(self.screen, SecretPage):
            self.confirm(
                "Leave? This key's secret won't be shown again.",
                lambda: self.open_confirmed(page),
                "Leave",
            )
            return
        if (
            isinstance(self.screen, FormPage)
            and self.screen.baseline is not None
            and self.screen.values() != self.screen.baseline
        ):
            self.confirm(
                "Discard unsaved changes and switch screens?",
                lambda: self.open_confirmed(page),
                "Discard",
            )
            return
        self.open_confirmed(page)

    def open_confirmed(self, page: str):
        while len(self.screen_stack) > 2:
            self.pop_screen()
        if page in TAB_PAGES:
            self.show_tab(page)
        else:
            self.switch_screen(self.make_page(page))

    def tab_page(self, name: str) -> tuple[RouterPage, bool]:
        """The live screen for a top-level tab, and whether it already ran."""
        screen = self.tab_screens.get(name)
        if screen is not None:
            return screen, True
        # Only plain tabs: pages built with options are one-offs.
        screen = self.make_page(name)
        # Unique per instance: a replaced tab (see account_changed) installs
        # its fresh screen while the old one is still leaving.
        self.install_screen(screen, f"tab-{name}-{id(screen)}")
        self.tab_screens[name] = screen
        return screen, False

    def show_tab(self, name: str):
        leaving = self.screen
        discarded = (
            isinstance(leaving, FormPage)
            and leaving.baseline is not None
            and leaving.values() != leaving.baseline
        )
        screen, cached = self.tab_page(name)
        self.switch_screen(screen)
        if discarded and leaving is not screen:
            # A discarded draft must not return with its cached form. Once
            # uninstalled, the switch above removes it like any other page.
            for tab, kept in list(self.tab_screens.items()):
                if kept is leaving:
                    del self.tab_screens[tab]
                    self.uninstall_screen(leaving)
        if cached:
            # Shown at once as it was left, then revalidated behind it.
            screen.fit_max_height()
            screen.action_focus_tabs()
            self.load_page(screen, quiet=True)

    def push(self, page: RouterPage):
        if self.busy:
            self.notice_busy()
        else:
            self.push_screen(page)

    def replace(self, page: RouterPage):
        self.switch_screen(page)

    def back(self, *, force: bool = False):
        if self.busy:
            if self.cancellable:
                self.cancel_event.set()
            else:
                self.notice_busy()
            return
        if (
            not force
            and isinstance(self.screen, FormPage)
            and self.screen.baseline is not None
            and self.screen.values() != self.screen.baseline
        ):
            self.confirm(
                "Discard unsaved changes?", lambda: self.back(force=True), "Discard"
            )
            return
        if len(self.screen_stack) > 2:
            self.pop_screen()
            # Forms retain their draft when a nested screen closes. Other
            # pages refresh behind what they already show.
            if isinstance(self.screen, RouterPage) and not isinstance(
                self.screen, FormPage
            ):
                self.load_page(self.screen, quiet=True)
        elif isinstance(self.screen, HomePage):
            self.action_quit_router()
        else:
            self.show_tab("home")

    def confirm(
        self,
        message: str,
        operation: Callable,
        label: str = "Confirm",
        *,
        focus_accept: bool = False,
    ):
        if self.busy:
            self.notice_busy()
            return
        self.push_screen(
            Confirm(message, label, focus_accept=focus_accept),
            lambda approved: operation() if approved else None,
        )

    def notice_busy(self):
        if isinstance(self.screen, (RouterPage, ConnectPage)):
            self.screen.message(
                "An operation is in progress. Wait for its result; leaving will not undo it."
            )

    def action_quit_router(self):
        if self.busy:
            if self.cancellable:
                self.cancel_event.set()
            self.notice_busy()
            return
        if (
            isinstance(self.screen, FormPage)
            and self.screen.baseline is not None
            and self.screen.values() != self.screen.baseline
        ):
            self.confirm("Discard unsaved changes and quit?", self.exit, "Quit")
        elif isinstance(self.screen, SecretPage):
            self.confirm(
                "Quit? This key's secret won't be shown again.", self.exit, "Quit"
            )
        else:
            self.exit()

    @property
    def busy(self) -> bool:
        return self._busy

    @busy.setter
    def busy(self, value: bool):
        self._busy = value
        self.sync_activity()

    @property
    def active(self) -> bool:
        """A change or a read is in flight: the spinner runs."""
        return self._busy or self.loads > 0

    def sync_activity(self):
        timer = getattr(self, "_activity_timer", None)
        if timer is None:
            return
        if self.active and self.app_focus:
            timer.resume()
        else:
            timer.pause()
            # Clear the spinner now rather than on a tick that won't come.
            self.animate()

    def animate(self):
        if not self.screen_stack:
            return
        screen = self.screen_stack[-1]
        value = ""
        if self.active:
            elapsed = time.monotonic() - self.started
            frame = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(elapsed / 0.1) % 10] * 4
            if self.reduced_motion:
                frame = "····"
            # Fixed-width dots keep the centered label from shifting.
            dots = ("." * (int(elapsed / 0.5) % 3 + 1)).ljust(3, "\u00a0")
            value = f"{frame} {self.operation_label}{dots} ({elapsed:.1f}s)"
        if isinstance(screen, ConnectPage):
            # The dialog stays up while it works, so progress takes its status
            # line; the result replaces it, so an idle tick leaves it alone.
            if value:
                screen.progress(value)
        elif isinstance(screen, RouterPage):
            # The timer may tick while a screen is mounting or tearing down.
            # Repaint only on change, and skip layout: the size ignores the text.
            for activity in screen.query("#activity, #home-activity").results(Static):
                if activity.content != value:
                    activity.update(value, layout=False)

    def action_toggle_motion(self):
        self.reduced_motion = not self.reduced_motion
        for screen in self.screen_stack:
            for widget in screen.query("BrandAnimation, Marquee"):
                widget.sync()
        if isinstance(self.screen, RouterPage):
            self.screen.message(
                "Motion reduced." if self.reduced_motion else "Motion enabled."
            )

    def with_write(
        self, page: RouterPage, read: Callable, complete: Callable | None
    ) -> tuple[str, Callable, Callable]:
        """Fold the change run_and_reload queued into the read that follows it."""
        (label, write, note), self.pending_write = self.pending_write, None

        def operation():
            try:
                write()
            except Exception as error:
                # A change made of several requests (a strategy's key moves)
                # can fail partway, so show what Router now has, then the error.
                try:
                    return read(), error
                except Exception:
                    raise UnconfirmedWrite(
                        f"{str(error).rstrip('.')}. Router's current state "
                        "couldn't be reloaded; "
                        "press r to reload before making more changes."
                    ) from error
            try:
                return read(), None
            except Exception as error:
                raise RampCLIError(
                    f"{note or 'Saved.'} Refreshing failed: {error}"
                ) from error

        def done(outcome):
            result, error = outcome
            if complete is not None:
                complete(result)
            if not page.is_mounted:
                return
            if error is not None:
                page.message(str(error), error=True)
            elif note:
                page.message(note)

        return label, operation, done

    def run_operation(
        self,
        page: RouterPage,
        label: str,
        operation: Callable,
        complete: Callable | None = None,
        *,
        cancellable: bool = False,
        read: bool = False,
    ):
        if self.busy:
            self.notice_busy()
            return
        read, quiet = self.reading or read, self.quiet_load and page.content_shown
        if not read or self.pending_write is not None:
            self.generation += 1
        generation = self.generation
        # A plain read changes nothing, so it holds no lock: switching tabs or
        # starting another read never waits on it. Changes, and reads carrying
        # one, still lock.
        background = read and self.pending_write is None and not cancellable
        if self.pending_write is not None:
            label, operation, complete = self.with_write(page, operation, complete)
        # A newer read supersedes this page's in-flight one: cancelling its
        # worker drops the result at the await (the thread finishes unseen).
        group = f"load-{id(page)}"
        # Restore what an earlier read on this page disabled first: cancelling
        # it skips its own cleanup, and freezing below would record those
        # controls as disabled and leave them that way.
        self.release_frozen(page)
        if read:
            self.workers.cancel_group(self, group)
        if page.loading_timer is not None:
            page.loading_timer.stop()
            page.loading_timer = None
        if background:
            self.loads += 1
            self.sync_activity()
        else:
            self.busy = True
            self.cancellable = cancellable
            self.cancel_event.clear()
        self.started = time.monotonic()
        self.operation_label = label
        page.message(label + "…")
        # Disabling the focused control drops focus, so remember it to give back.
        # A chained operation inherits the control the last one was restoring.
        focused = (
            self.restoring_focus or getattr(page, "pending_focus", None) or page.focused
        )
        # (widget, was_disabled) pairs to restore; filled now or once slow.
        disabled: list[tuple[Widget, bool]] = []
        if background:
            self.frozen[id(page)] = disabled
        hidden = [False]

        def hide():
            hidden[0] = True
            self.set_loading(page, True)
            # Home's wordmark keeps the action row in view; it waits disabled.
            if page.has_class("hero-loading"):
                for button in page.query("#page-actions Button"):
                    disabled.append((button, button.disabled))
                    button.disabled = True

        def slow():
            page.loading_timer = None
            if not quiet:
                hide()
            if read and not background:
                disabled.extend(self.freeze(page, cancellable))

        # Sign-in keeps its page visible: progress shows the URL and code.
        # Freeze editing and submission, not the event loop. Cancel sign-in
        # stays enabled; Tab/scroll/resize remain responsive. Reads skip the
        # freeze until slow: busy already rejects other operations, and a
        # cached answer then finishes without restyling every control.
        if cancellable:
            disabled.extend(self.freeze(page, cancellable))
        elif not page.content_shown:
            # Hidden content can't be used, so a first load needs no freeze.
            hide()
            if not read:
                disabled.extend(self.freeze(page, cancellable))
        else:
            # Waits briefly so fast operations never blank visible content.
            page.loading_timer = page.set_timer(0.15, slow)
            if not read:
                disabled.extend(self.freeze(page, cancellable))
        account = self.account_identity()
        if read and account != self.account:
            # The login changed since the last result, and cache keys don't
            # name the account: clear them before this read can be answered
            # from the previous account's entries.
            self.service.invalidate()
        self.run_worker(
            self.perform(
                page,
                operation,
                complete,
                disabled,
                focused,
                hidden,
                background,
                read=read,
                account=account,
                generation=generation,
            ),
            group=group if background else "default",
            exit_on_error=False,
        )

    def freeze(self, page, cancellable: bool) -> list[tuple[Widget, bool]]:
        frozen = []
        for widget in page.query("Button, Input, Select, Checkbox, SelectionList"):
            # Sign-in stays cancellable and can be finished from a pasted address.
            if (
                widget.id in ("cancel-login", "callback-url", "copy-login-url")
                and cancellable
            ):
                continue
            frozen.append((widget, widget.disabled))
            widget.disabled = True
        return frozen

    def set_loading(self, page, loading: bool):
        """Toggle a page's loading class without restyling the whole page.

        Only the frame's parts, the action row, and Home's tagline slot read the
        class (see the `RouterPage.loading` rules), so restyle just those.
        """
        if not isinstance(page, RouterPage):
            # Modals like Connect have their own layout; restyle them whole.
            page.set_class(loading, "loading")
            return
        if loading:
            name = page.loading_style()
            if page.has_class(name):
                return
        else:
            name = next(
                (name for name in ("loading", "hero-loading") if page.has_class(name)),
                None,
            )
            if name is None:
                return
        if not page.is_mounted:
            # A first load starts from on_mount, before styles are applied.
            page.set_class(loading, name)
            return
        page.set_class(loading, name, update=False)
        nodes = []
        for node in page.query(
            "#activity, #status-row, #body, #page-actions, #home-tagline, #home-activity"
        ):
            nodes.append(node)
            # Visibility is inherited, so the action buttons follow their row.
            if node.id == "page-actions":
                nodes.extend(node.walk_children())
        self.stylesheet.update_nodes(nodes, animate=False)
        if name == "hero-loading":
            if loading:
                page.call_after_refresh(page.place_activity)
            else:
                # Re-placed next time: the frame may have resized meanwhile.
                # The refresh spinner reuses this widget in its own slot, so
                # drop the lift too, or it draws above that slot, out of view.
                activity = page.query_one("#home-activity")
                activity.remove_class("placed")
                activity.styles.offset = (0, 0)

    def load_page(self, page, *, quiet: bool = False):
        """Load a page; quietly keeps its current content in view meanwhile."""
        self.reading, self.quiet_load = True, quiet
        try:
            page.load()
        finally:
            self.reading = self.quiet_load = False

    def live(self, page) -> bool:
        """Whether a page is still shown or kept as a tab, not left behind.

        A left page stays mounted while it's torn down, after its widgets go.
        """
        return page.is_mounted and (
            page in self.screen_stack or page in self.tab_screens.values()
        )

    def account_identity(self) -> tuple | None:
        try:
            return self.service.identity()
        except (AttributeError, RampCLIError, OSError, ValueError):
            return None

    def account_changed(self):
        """Drop every screen holding the previous account's data.

        Cached tabs and pages pushed over them are rebuilt on the next visit,
        so a reload that fails can never leave another account's rows in view.
        """
        # Cache keys don't name the account: clear them first. The new cache
        # generation also keeps in-flight reads from being stored or joined.
        self.service.invalidate()
        stale = [
            screen
            for screen in self.tab_screens.values()
            if not isinstance(screen, AccountPage)
        ]
        self.tab_screens = {
            tab: screen
            for tab, screen in self.tab_screens.items()
            if screen not in stale
        }
        current = self.screen
        if isinstance(current, RouterPage) and not isinstance(current, AccountPage):
            while len(self.screen_stack) > 2:
                self.pop_screen()
            self.show_tab(current.tab_section)
        for screen in stale:
            if screen not in self.screen_stack:
                self.uninstall_screen(screen)

    async def perform(
        self,
        page,
        operation,
        complete,
        disabled,
        focused,
        hidden,
        background,
        *,
        read: bool = False,
        account: tuple | None = None,
        generation: int = 0,
    ):
        result = None
        error = None
        try:
            result = await asyncio.to_thread(operation)
        except Exception as caught:
            error = caught
        finally:
            if background:
                self.loads -= 1
                self.sync_activity()
            else:
                self.busy = False
                self.cancellable = False
        # A read answered locally (signed out, say) can finish before its
        # page's own mount has; wait for it, or the result is dropped as
        # coming from a page that was left.
        for _ in range(100):
            if page.is_mounted or not (
                page in self.screen_stack or page in self.tab_screens.values()
            ):
                break
            await asyncio.sleep(0.01)
        if self.frozen.get(id(page)) is disabled:
            del self.frozen[id(page)]
        now = self.account_identity()
        # Started before a change or for another account: never shown.
        stale = read and (account != now or generation != self.generation)
        if now != self.account:
            # Before anything is revealed: screens holding the previous
            # account's rows are dropped, never shown with an error over them.
            self.account = now
            self.account_changed()
        if page.loading_timer is not None:
            page.loading_timer.stop()
            page.loading_timer = None
        # Reveal and repopulate in one repaint so the page never flashes
        # stale or empty content between the spinner and the result.
        with self.batch_update():
            # Newest first, so a widget frozen twice ends as it began.
            for widget, was_disabled in reversed(disabled):
                if widget.is_mounted and widget.disabled != was_disabled:
                    widget.disabled = was_disabled
            # A page left mid-load drops its result: its widgets are gone.
            if self.live(page) and not stale:
                self.set_loading(page, False)
                page.remove_class("refreshing")
                page.content_shown = True
                # Before finishing, so a result that moves focus still wins.
                # Only a freeze or a hide takes focus; otherwise it stays put.
                if (
                    (disabled or hidden[0])
                    and focused is not None
                    and focused.is_mounted
                ):
                    page.restore_focus(focused)
                # Focus lands a tick later, so an operation chained from the
                # result would otherwise capture whatever held focus meanwhile.
                self.restoring_focus = focused
                try:
                    page.applied_generation = generation
                    self.finish(page, result, error, complete, read=read)
                finally:
                    self.restoring_focus = None
        # A stale read's page reloads unless a newer result already landed.
        if (
            stale
            and self.live(page)
            and not self.busy
            and getattr(page, "applied_generation", -1) < self.generation
        ):
            self.load_page(page, quiet=page.content_shown)

    def release_frozen(self, page):
        frozen = self.frozen.pop(id(page), None) or []
        for widget, was_disabled in reversed(frozen):
            if widget.is_mounted and widget.disabled != was_disabled:
                widget.disabled = was_disabled
        # Emptied in place: the read that filled it restores nothing later.
        frozen.clear()

    def finish(self, page, result, error, complete, *, read: bool = False):
        # The content and its action row; the tabs and r (reload) stay live.
        editable = page.query("#body, #page-actions")
        if isinstance(error, UnconfirmedWrite):
            # What's shown may no longer match Router, and a second create
            # could duplicate the first: nothing acts until a reload succeeds.
            editable.set(disabled=True)
        elif error is None and read:
            editable.set(disabled=False)
        if error is not None:
            page.message(str(error), error=True)
            uncertain = isinstance(error, httpx.TransportError) or (
                isinstance(error, ApiError) and error.status_code >= 500
            )
            # Only a create request can leave a key half-made; a failed read of
            # the page's strategies submitted nothing.
            if isinstance(page, CreateKeyPage) and not read:
                page.uncertain = uncertain
                if not uncertain:
                    page.request_id = str(uuid4())
            if isinstance(page, AccountPage):
                page.query_one("#cancel-login", Button).disabled = True
                page.query_one("#cancel-login", Button).display = False
                page.query_one("#login-progress", Static).display = False
                page.hide_callback_input()
                if not page.query_one("#home-content").has_class("signed-in"):
                    # A failed or cancelled sign-in returns to the centered
                    # Sign in, with the reason in place of the prompt.
                    page.message("")
                    page.query_one("#auth-animation", BrandAnimation).start()
                    page.show_signed_out(str(error))
        elif complete is not None:
            complete(result)
        else:
            page.message("Completed.")


class UnconfirmedWrite(RampCLIError):
    """A change failed, possibly partway, and the reload after it failed too."""


# Pages and widgets moved to their own modules; keep importing
# them from here working.
__all__ = [
    "ARROW_BINDINGS",
    "AccountPage",
    "ArrowNavigation",
    "Button",
    "CHART_GROW_SECONDS",
    "CellChecklist",
    "CellEditing",
    "CellInput",
    "CellMenu",
    "CellTable",
    "ChoiceDialog",
    "CollectionPage",
    "Confirm",
    "ConnectPage",
    "CreateKeyPage",
    "DisconnectPage",
    "FormPage",
    "HarnessModelPage",
    "HarnessRoutingPage",
    "HarnessesPage",
    "HomePage",
    "InlineModal",
    "KeyEditPage",
    "KeyHints",
    "KeyPage",
    "KeyTable",
    "KeysPage",
    "LegacyPage",
    "MIN_CHART_ROWS",
    "Marquee",
    "MetricDivider",
    "Notice",
    "PAGES",
    "PaletteText",
    "REFRESHING",
    "ResultPage",
    "RouterApp",
    "RouterFooter",
    "RouterPage",
    "RouterTable",
    "RouterTabs",
    "SecretPage",
    "StrategiesPage",
    "StrategyTable",
    "TAB_PAGES",
    "WIDE_METRICS",
    "_latency",
    "_tokens",
    "arrow_owned",
    "arrow_row",
    "button_styles",
    "centred_width",
    "focus_first_action",
    "harness_labels",
    "httpx",
    "inherited_label",
    "key_count",
    "parse_key_name",
    "parse_spend_cap",
    "parse_strategy_name",
    "scroll_area",
    "title_case",
    "usage_latency",
]
