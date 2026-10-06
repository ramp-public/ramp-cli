"""The Home and Account tab."""

import re
import time
from urllib.parse import urlsplit

import click
from rich.style import Style
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.containers import (
    Horizontal,
    Vertical,
)
from textual.widgets import (
    Input,
    Select,
    Static,
)

from ramp_cli.client.router_user import validate_origin
from ramp_cli.commands import router
from ramp_cli.commands.router_user import (
    _credits,
    _money,
)
from ramp_cli.router_ui.animation import (
    BrandAnimation,
    wordmark_bottom,
)
from ramp_cli.router_ui.chart import chart_series, usage_chart, usage_legend
from ramp_cli.router_ui.pages import (
    RouterPage,
)
from ramp_cli.router_ui.service import (
    USAGE_WINDOWS,
)
from ramp_cli.router_ui.widgets import (
    CHART_GROW_SECONDS,
    MIN_CHART_ROWS,
    REFRESHING,
    WIDE_METRICS,
    Button,
    CellInput,
    Marquee,
    MetricDivider,
    title_case,
)

_URL = re.compile(r"https?://\S+")
# Wordmark rows below which a waiting sign-in hides the empty metric cells.
MIN_SIGN_IN_HERO = 5


def _linked(message: str) -> Text:
    """``message`` with each URL a terminal hyperlink (OSC 8).

    Terminals detect links on one screen line, so a wrapped URL opens only
    its first line; a hyperlink names the whole URL on every line it covers.
    Terminals without hyperlinks show the same plain text as before.
    """
    text = Text(message)
    for match in _URL.finditer(message):
        text.stylize(Style(link=match.group(0), underline=True), *match.span())
    return text


class AccountPage(RouterPage):
    page_title = "account"

    def __init__(
        self,
        origin: str | None = None,
        sign_in: bool = False,
        no_browser: bool = False,
        timeout: int = 900,
    ):
        super().__init__()
        self.origin = origin
        self.sign_in = sign_in
        self.no_browser = no_browser
        self.timeout = timeout
        # None until usage loads: the service picks the longest window the
        # history fills. A window the user picks then sticks across reloads.
        self.days: int | None = None
        self.usage: dict | None = None
        # Bars grow in on the first load and a new window, not a return visit.
        self.grow_chart_in = True
        # Frame rows that aren't chart, keyed by compact layout.
        self.chart_overhead: dict[bool, int] = {}
        self.placing = 0
        self.moving = 0
        self.revealing = 0
        # Shown once by the next load, after a sign-in or a Router switch.
        self.notice = ""
        # The browser sign-in awaiting its callback, which a pasted address
        # can finish (see callback_pasted).
        self.pending_callback = None
        # The sign-in URL on screen, for Copy link.
        self.login_url: str | None = None

    def content(self) -> ComposeResult:
        with Vertical(id="home-content"):
            with Vertical(id="home-hero"):
                yield BrandAnimation(
                    id="auth-animation", rows=10, ambient=True, max_width=88
                )
            yield Static("Tokens are money. Save both.", id="home-tagline")
            # Signed out by Router, the reason and a focused Sign in replace it.
            yield Static("", id="home-notice", markup=False)
            with Horizontal(id="home-signin"):
                yield Button("↵ Sign in", id="home-login")
            # While loading signed out, the wordmark is the loading screen and
            # the spinner takes the tagline's place (see `place_activity`).
            yield Static("", id="home-activity", markup=False)
            with Vertical(id="home-usage"):
                # The bordered picker sits right of the legend, which is centered beside it.
                with Horizontal(id="usage-header"):
                    yield Marquee(id="usage-legend")
                    yield Select(
                        [(label, days) for days, label in USAGE_WINDOWS],
                        value=self.days or 7,
                        allow_blank=False,
                        id="usage-days",
                    )
                yield BrandAnimation(
                    id="usage-empty", rows=8, ambient=True, max_width=88
                )
                with Vertical(id="usage-scroll"):
                    yield Static("", id="usage-chart")
            yield Static("", id="login-progress", markup=False)
            # A browser that can't reach this machine (a container, SSH) can't
            # finish sign-in itself; its address can be pasted here instead.
            yield Input(
                placeholder="Browser can't reach this machine? Paste the address it was sent to",
                id="callback-url",
            )
            with Vertical(id="home-summary-wrap"):
                with Vertical(id="account-summary"):
                    with Horizontal(classes="metric-row"):
                        with Vertical(classes="account-metric"):
                            yield Static("ACCOUNT", classes="metric-label")
                            yield Static(
                                "Checking account…", id="account-details", markup=False
                            )
                        with Vertical(classes="account-metric"):
                            yield Static("AVAILABLE CREDITS", classes="metric-label")
                            yield Static("—", id="account-credits", markup=False)
                        with Vertical(classes="account-metric wide-metric"):
                            yield Static(
                                "TOKENS TODAY",
                                id="tokens-label",
                                classes="metric-label",
                            )
                            yield Static("—", id="window-tokens", markup=False)
                    yield MetricDivider(id="metric-divider")
                    with Horizontal(classes="metric-row last-row"):
                        with Vertical(classes="account-metric"):
                            yield Static(
                                "SPENT TODAY", id="spent-label", classes="metric-label"
                            )
                            yield Static("—", id="today-spent", markup=False)
                        with Vertical(classes="account-metric"):
                            yield Static(
                                "SAVED TODAY", id="saved-label", classes="metric-label"
                            )
                            yield Static("—", id="today-saved", markup=False)
                        # The API reports latency, not tokens per second.
                        with Vertical(classes="account-metric wide-metric"):
                            yield Static("LATENCY", classes="metric-label")
                            yield Static("—", id="window-latency", markup=False)

    def on_mount(self):
        super().on_mount()
        self.call_after_refresh(self.fit_home)

    def on_resize(self):
        self.call_after_refresh(self.fit_home)
        self.draw_usage()

    def message(self, message: str, *, error: bool = False):
        # A refresh shows the centered spinner instead of a status line.
        refreshing = message == f"{REFRESHING}…" and self.router_app.active
        self.set_class(refreshing, "refreshing")
        super().message("" if refreshing else message, error=error)
        if self.is_mounted:
            self.query_one("#status").set_class(bool(message), "visible")
            self.call_after_refresh(self.fit_home)

    def fit_home(self):
        if not self.is_mounted:
            return
        # Size from the frame, not #body: the frame keeps its size while
        # loading hides the body, so the hero is already right on reveal.
        frame = self.query_one("#frame")
        status = self.query_one("#status")
        progress = self.query_one("#login-progress")
        full = frame.content_size.height
        # Key hints live in the top bar, so the frame is the page's own.
        if status.has_class("visible"):
            height = full - status.outer_size.height
        else:
            height = full
        extra = progress.region.height if progress.display else 0
        field = self.query_one("#callback-url")
        if field.display:
            extra += field.outer_size.height
        compact = height < 20
        content = self.query_one("#home-content")
        if content.has_class("signed-out") and not (
            self.has_class("hero-loading") or self.has_class("refreshing")
        ):
            # The centered Sign in and its margin.
            extra += 4
        content.set_class(compact, "compact")
        self.call_after_refresh(self.reveal)
        self.fit_metrics(frame.content_size.width >= WIDE_METRICS)
        if content.has_class("signed-in"):
            # Everything but the bars takes a fixed share of the frame; measure
            # it while the chart shows so the hidden chart knows when to return.
            box = self.query_one("#usage-scroll")
            if not content.has_class("no-chart") and box.display and box.size.height:
                body = self.query_one("#body").content_size.height
                self.chart_overhead[compact] = body - box.size.height
            overhead = self.chart_overhead.get(compact)
            if overhead is not None:
                # Too few rows to read and the metric cells stand alone.
                content.set_class(height - overhead < MIN_CHART_ROWS, "no-chart")
            # The chart fills its area, so it redraws whenever that area changes.
            self.call_after_refresh(self.draw_usage)
            return
        # Below the hero: the tagline (or activity) and its margin, then the
        # metric cells (7 rows compact, 9 otherwise) and the row under them.
        rows = max(1, height - (10 if compact else 12) - extra)
        # Signing in, the empty metric cells give way when the steps and the
        # cells can't both fit; they hold nothing until sign-in finishes.
        squeezed = content.has_class("signing-in") and rows < MIN_SIGN_IN_HERO
        content.set_class(squeezed, "squeezed")
        if squeezed:
            rows = max(1, height - 2 - extra)
        animation = self.query_one("#auth-animation", BrandAnimation)
        animation.max_width = frame.content_size.width
        if animation.rows != rows:
            animation.rows = rows
            self.query_one("#home-hero").styles.height = rows
            animation.styles.height = 3 if animation.success else rows
            animation.draw()
        if self.has_class("hero-loading"):
            self.call_after_refresh(self.place_activity)
        if content.has_class("signed-out"):
            self.call_after_refresh(self.place_notice)
        if content.has_class("signing-in"):
            self.call_after_refresh(
                self.place_notice, 0, "#login-progress, #callback-url"
            )

    def reveal(self):
        """Show the content once the first fit has settled.

        Hidden until then, so the opening frames never show the boxes jump as the
        hero is sized, or the boxes before the wordmark has painted.
        """
        content = self.query_one("#home-content")
        if content.has_class("fitted"):
            return
        animation = self.query_one("#auth-animation", BrandAnimation)
        width = min(animation.content_size.width, animation.max_width)
        painting = (
            not content.has_class("signed-in")
            and animation.display
            and (
                animation.content_region.height != animation.rows
                or animation.frame_key is None
                or animation.frame_key[0] != width
            )
        )
        # Signed out and loading, the spinner shows with the wordmark or not at all.
        unplaced = self.has_class("hero-loading") and not self.query_one(
            "#home-activity"
        ).has_class("placed")
        if (painting or unplaced) and self.revealing < 5:
            self.revealing += 1
            self.call_after_refresh(self.reveal)
            return
        content.add_class("fitted")

    def loading_style(self) -> str:
        # Signed out, the wordmark stays up as the loading screen.
        if self.query_one("#home-content").has_class("signed-in"):
            return "loading"
        return "hero-loading"

    def place_activity(self):
        """Tuck the spinner just under the wordmark, one blank row between."""
        if not self.is_mounted or not self.has_class("hero-loading"):
            return
        activity = self.query_one("#home-activity")
        animation = self.query_one("#auth-animation", BrandAnimation)
        boxes = self.query_one("#home-summary-wrap")
        if not animation.display or animation.success:
            return
        # Wait for layout to catch up with a new hero size; placing from the old
        # size would flash the spinner over the wordmark.
        hero = self.query_one("#home-hero")
        settled = (
            hero.region.height == animation.content_region.height == animation.rows
        )
        if not settled and self.placing < 5:
            self.placing += 1
            self.call_after_refresh(self.place_activity)
            return
        self.placing = 0
        # The animation draws in its first `max_width` columns of content.
        width = min(animation.content_size.width, animation.max_width)
        art = animation.content_region.y + wordmark_bottom(width, animation.rows)
        # Never past the gap's midpoint, so it still reads as part of the hero.
        target = min(art + 2, (art + boxes.region.y + 1) // 2)
        # Laid out right below the hero, in the tagline's slot; offset from there.
        slot = hero.region.bottom
        offset = min(0, target - slot)
        if int(activity.styles.offset.y.value) != offset:
            activity.styles.offset = (0, offset)
            # Offsets don't reflow on their own; the region updates next layout.
            self.screen.refresh(layout=True)
            # Hide it until the move has painted, or it flashes in the old
            # slot. Bounded, so a layout that never settles can't spin forever.
            if self.moving < 3:
                self.moving += 1
                activity.remove_class("placed")
                self.set_timer(0.05, self.place_activity)
                return
        self.moving = 0
        # Transparent, not undisplayed, until placed, so revealing it moves nothing.
        activity.add_class("placed")
        self.reveal()

    def show_signed_out(self, notice: str):
        """Center ``notice`` and a focused Sign in under the wordmark."""
        self.query_one("#home-content").add_class("signed-out")
        # Shown under the wordmark, above the button, not as a status line.
        self.query_one("#home-notice", Static).update(notice)
        if not self.sign_in:
            self.call_after_refresh(self.query_one("#home-login", Button).focus)
        self.call_after_refresh(self.fit_home)

    def place_notice(
        self, tries: int = 0, selector: str = "#home-notice, #home-signin"
    ):
        """Lift the signed-out notice and Sign in, or the sign-in steps, to
        just under the wordmark."""
        if not self.is_mounted:
            return
        widgets = list(self.query(selector))
        animation = self.query_one("#auth-animation", BrandAnimation)
        hero = self.query_one("#home-hero")
        if not widgets[0].display or not animation.display or animation.success:
            return
        # As with the spinner, placing from a stale hero size would misplace them.
        settled = (
            hero.region.height == animation.content_region.height == animation.rows
        )
        if not settled and tries < 5:
            self.call_after_refresh(self.place_notice, tries + 1, selector)
            return
        width = min(animation.content_size.width, animation.max_width)
        art = animation.content_region.y + wordmark_bottom(width, animation.rows)
        # One blank row under the ink; laid out from the hero's bottom edge.
        offset = min(0, art + 2 - hero.region.bottom)
        for widget in widgets:
            if int(widget.styles.offset.y.value) != offset:
                widget.styles.offset = (0, offset)
            widget.add_class("placed")

    def fit_metrics(self, wide: bool):
        # Wide frames add tokens and latency as a third column (3×2, not 2×2).
        for card in self.query(".wide-metric"):
            card.display = wide
        for row in self.query(".metric-row"):
            cards = [card for card in row.query(".account-metric") if card.display]
            for card in cards:
                card.set_class(card is cards[-1], "row-end")
        self.query_one("#metric-divider").refresh()

    def actions(self) -> ComposeResult:
        # Hidden until a load finds the user signed out.
        login = Button("Sign in", id="login")
        login.display = False
        yield login
        yield Button("Sign out", id="logout")
        yield Button("Cancel sign-in", id="cancel-login", disabled=True)
        # A wrapped link may only open its first line; this copies all of it.
        yield Button("Copy link", id="copy-login-url")
        yield Static("", classes="spacer")
        yield Button("Settings", id="settings")

    def load(self):
        animation = self.query_one("#auth-animation", BrandAnimation)
        if not animation.display:
            animation.start()
        # Coming back to Home refreshes data the TUI already showed once.
        self.grow_chart_in = not self.router_app.home_loaded
        self.run(
            REFRESHING if self.router_app.home_loaded else "Starting up",
            lambda: self.service.account(self.days),
            self.loaded,
        )

    def loaded(self, data):
        self.router_app.home_loaded = True
        self.router_app.update_identity(data["session"])
        session = data["session"]
        credits = (
            _credits(data["billing"], self.service.client.origin)
            if data.get("billing") is not None
            else "—"
        )
        if credits == "Unavailable":
            credits = "—"
        self.query_one("#account-details", Static).update(
            self.router_app.identity
            if session.get("authenticated")
            else "Not signed in"
        )
        self.query_one("#account-credits", Static).update(credits)
        authenticated = bool(session.get("authenticated"))
        if authenticated:
            # Home's own reads are done; warm the other tabs behind it.
            self.service.prefetch_tabs()
        self.query_one("#home-content").set_class(authenticated, "signed-in")
        animation = self.query_one("#auth-animation", BrandAnimation)
        if authenticated:
            animation.stop()
        elif not animation.display:
            animation.start()
        self.show_usage(data.get("usage") if authenticated else None)
        self.query_one("#login-progress", Static).display = False
        # Signed out, for whatever reason, Home centers why and a focused
        # Sign in under the wordmark instead of offering empty stats.
        signed_out = not authenticated
        content = self.query_one("#home-content")
        content.set_class(signed_out, "signed-out")
        # The centered Sign in replaces the one in the action row.
        self.query_one("#login", Button).display = False
        self.query_one("#logout", Button).display = authenticated
        self.query_one("#logout", Button).disabled = not authenticated
        error = data.get("billing_error") or data.get("usage_error") or ""
        notice, self.notice = self.notice, ""
        if signed_out:
            host = urlsplit(self.service.client.origin).hostname
            self.show_signed_out(
                notice
                or (
                    f"Your sign-in to {host} ended. Sign in to continue."
                    if data.get("signed_out")
                    else f"Sign in to {host} to see your credits, spend, and savings."
                )
            )
            notice = ""
        self.message(error or notice, error=bool(error))
        self.call_after_refresh(self.fit_home)
        if self.sign_in:
            self.sign_in = False
            self.login()

    def show_usage(self, usage: dict | None):
        self.usage = usage
        self.ready = True
        if usage:
            # Showing the window isn't choosing one, so it must not reload.
            self.days = usage["days"]
            picker = self.query_one("#usage-days", Select)
            with picker.prevent(Select.Changed):
                picker.value = self.days
        summary = (usage or {}).get("summary") or {}
        days = self.days or 1
        label = dict(USAGE_WINDOWS)[days]
        window = "today" if days == 1 else f"last {label}"
        for name in ("spent", "saved", "tokens"):
            self.query_one(f"#{name}-label", Static).update(
                title_case(f"{name} {window}".upper())
            )
        self.query_one("#today-spent", Static).update(
            _money(summary.get("spend_usd")) if usage else "—"
        )
        self.query_one("#today-saved", Static).update(
            _money(summary.get("cost_savings_usd")) if usage else "—"
        )
        self.query_one("#window-tokens", Static).update(
            _tokens(summary.get("total_tokens")) if usage else "—"
        )
        self.query_one("#window-latency", Static).update(usage_latency(summary))
        # Only new data restarts the marquee; resizes keep its place.
        self.query_one("#usage-legend", Marquee).update(
            usage_legend(usage or {}, self.router_app.palette)
        )
        self.draw_usage()

    def palette_changed(self):
        # The chart and legend bake their colors into the text, so redraw both.
        if self.is_mounted and self.usage is not None:
            self.query_one("#usage-legend", Marquee).update(
                usage_legend(self.usage, self.router_app.palette)
            )
            self.draw_usage()

    def reload_usage(self):
        self.grow_chart_in = True

        def loaded(usage):
            self.show_usage(usage)
            self.message("")

        self.run(
            "Loading usage",
            lambda: self.service.daily_usage(self.days),
            loaded,
            # A fetch like the page's load, so a login change since then
            # clears the cache first and an outdated answer is dropped.
            read=True,
        )

    def draw_usage(self):
        if not self.is_mounted or self.usage is None:
            return
        # No usage at all shows the brand animation where the chart would be.
        empty = not chart_series(self.usage)
        scroll = self.query_one("#usage-scroll")
        animation = self.query_one("#usage-empty", BrandAnimation)
        # Hidden, not removed, so the picker keeps its right-aligned slot.
        legend = self.query_one("#usage-legend", Marquee)
        legend.visible = not empty
        legend.sync()
        if empty:
            self.stop_chart_growth()
            scroll.display = False
            area = self.query_one("#home-usage").content_size.height
            controls = self.query_one("#usage-header").outer_size.height
            rows = max(3, area - controls - 2)
            if not animation.display or animation.rows != rows:
                animation.rows = rows
                animation.start()
            return
        animation.stop()
        if not scroll.display:
            # The chart sizes itself to the scroll area, so wait for its layout.
            scroll.display = True
            self.call_after_refresh(self.draw_usage)
            return
        size = scroll.content_size
        if not size.height:
            return
        # A resize reaches here from both on_resize and fit_home; only a new
        # area, palette, or dataset needs the chart rebuilt.
        palette = self.router_app.palette
        drawn = getattr(self, "_drawn_chart", None)
        if (
            drawn is not None
            and drawn[0] is self.usage
            and drawn[1:] == (palette, size.width, size.height)
        ):
            return
        # New data grows in from the baseline; a resize or palette change
        # (including one mid-animation) just draws the finished chart.
        fresh = self.grow_chart_in and (drawn is None or drawn[0] is not self.usage)
        self._drawn_chart = (self.usage, palette, size.width, size.height)
        self.stop_chart_growth()
        if fresh:
            self._grow_start = time.monotonic()
            self._grow_timer = self.set_interval(1 / 30, self.grow_chart)
        self.paint_chart(0.0 if fresh else 1.0)

    def paint_chart(self, progress: float):
        usage, palette, width, height = self._drawn_chart
        self.query_one("#usage-chart", Static).update(
            usage_chart(usage, palette, max(40, width), height, progress)
        )

    def grow_chart(self):
        elapsed = (time.monotonic() - self._grow_start) / CHART_GROW_SECONDS
        done = elapsed >= 1
        # Ease-out cubic: quick rise that settles into the final height.
        self.paint_chart(1.0 if done else 1 - (1 - elapsed) ** 3)
        if done:
            self.stop_chart_growth()

    def stop_chart_growth(self):
        timer = getattr(self, "_grow_timer", None)
        if timer is not None:
            timer.stop()
            self._grow_timer = None

    @on(Select.Changed, "#usage-days")
    def days_changed(self, event: Select.Changed):
        # A queued event from an earlier value is stale, not a new choice.
        if event.value != event.select.value:
            return
        if self.ready and event.value != self.days and not self.router_app.busy:
            self.days = int(event.value)
            # Signed out there is no usage to show; the next load uses the window.
            if self.query_one("#home-content").has_class("signed-in"):
                self.reload_usage()

    @on(Button.Pressed, "#login")
    def login(self):
        self.connect(self.origin or self.service.client.origin)

    @on(Button.Pressed, "#home-login")
    def home_login(self):
        self.login()

    def connect(self, origin: str, previous: str | None = None):
        """Sign in to ``origin``.

        From Settings, ``previous`` is the Router to stay on if it fails, and a
        sign-in that Router still honors is reused. An explicit sign-in always
        authorizes afresh: the saved grant may pass Router's session check yet
        be unusable, as when its business access was revoked.
        """
        no_browser = self.no_browser
        self.query_one("#home-content").remove_class("signed-out")
        # The sign-in steps take the tagline's place under the wordmark.
        self.query_one("#home-content").add_class("signing-in")
        for widget in self.query("#home-notice, #home-signin"):
            widget.remove_class("placed")
        self.query_one("#login", Button).display = False
        self.query_one("#cancel-login", Button).display = True
        self.query_one("#cancel-login", Button).disabled = False

        def progress(message):
            self.app.call_from_thread(self.show_progress, message)

        def pending(callback):
            self.app.call_from_thread(self.show_callback_input, callback)

        def operation():
            try:
                return self.service.login(
                    origin,
                    progress,
                    self.router_app.cancel_event,
                    no_browser=no_browser,
                    timeout=self.timeout,
                    reuse=previous is not None,
                    on_callback=pending,
                )
            except Exception as error:
                if previous is None:
                    raise
                return error

        def done(result):
            self.hide_callback_input()
            self.query_one("#cancel-login", Button).disabled = True
            self.query_one("#cancel-login", Button).display = False
            if isinstance(result, Exception):
                # Nothing switched, so the Router it was on comes back.
                self.notice = f"{result} Still connected to {previous}."
                self.load()
                return
            # Signing in again goes here too, not to a --ui-url Router.
            self.origin = origin
            if previous is not None:
                self.notice = f"Connected to {origin}."
            self.query_one("#login-progress", Static).display = True
            self.query_one("#login-progress", Static).update("Signed in.")
            self.query_one("#auth-animation", BrandAnimation).start(success=True)
            self.load()

        self.query_one("#auth-animation", BrandAnimation).start()
        self.run(
            f"Connecting to {urlsplit(origin).hostname}"
            if previous is not None
            else "Waiting for browser sign-in",
            operation,
            done,
            cancellable=True,
        )

    def show_progress(self, message: str):
        if not self.is_mounted:
            return
        urls = _URL.findall(message)
        self.login_url = urls[0] if urls else None
        self.query_one("#copy-login-url", Button).display = bool(urls)
        self.query_one("#login-progress", Static).display = True
        self.query_one("#login-progress", Static).update(_linked(message))
        self.call_after_refresh(self.fit_home)

    @on(Button.Pressed, "#copy-login-url")
    def copy_login_url(self):
        url = self.login_url
        if not url:
            return
        # OSC 52 reaches the terminal's clipboard even over SSH or from a
        # container; the local helper covers terminals that ignore it.
        self.app.copy_to_clipboard(url)
        self.run_worker(lambda: router._copy_to_clipboard(url), thread=True)
        self.message("Copied the sign-in link.")

    def show_callback_input(self, callback):
        if not self.is_mounted:
            return
        self.pending_callback = callback
        field = self.query_one("#callback-url", Input)
        field.value = ""
        field.disabled = False
        field.display = True
        # Ready to paste into; Tab still reaches Cancel sign-in.
        self.call_after_refresh(field.focus)
        self.call_after_refresh(self.fit_home)

    def hide_callback_input(self):
        """Put away what only a waiting sign-in uses: the paste field and Copy link."""
        self.pending_callback = None
        self.login_url = None
        if self.is_mounted:
            self.query_one("#home-content").remove_class("signing-in", "squeezed")
            for widget in self.query("#login-progress, #callback-url"):
                widget.styles.offset = (0, 0)
            field = self.query_one("#callback-url", Input)
            field.value = ""
            field.display = False
            self.query_one("#copy-login-url", Button).display = False

    @on(Input.Submitted, "#callback-url")
    def callback_pasted(self, event: Input.Submitted):
        callback = self.pending_callback
        if callback is None or not event.value.strip():
            return
        try:
            callback.submit_url(event.value)
        except click.ClickException as error:
            self.message(error.format_message(), error=True)
            return
        event.input.disabled = True
        self.message("Finishing sign-in…")

    @on(Button.Pressed, "#cancel-login")
    def cancel_login(self):
        self.router_app.cancel_event.set()
        self.message(
            "Cancelling while waiting for authorization. If already authorized, sign-in will finish safely."
        )

    @on(Button.Pressed, "#logout")
    def logout(self):
        self.router_app.confirm(
            "Revoke this CLI's Router login? Inference keys and your browser session are unchanged.",
            lambda: self.run_and_reload("Signing out", self.service.logout),
            "Sign out",
        )

    @on(Button.Pressed, "#settings")
    def settings(self):
        current = self.service.client.origin

        def parse(text: str) -> str:
            try:
                text = text.strip()
                # A bare host is the HTTPS one; validate_origin rejects others.
                return validate_origin(text if "://" in text else f"https://{text}")
            except click.BadParameter as error:
                raise ValueError(error.message) from error

        def chosen(result: tuple | None):
            if result is None or result[0] == current:
                return
            # Clear this Router's account first: nothing of it may show while
            # the next one connects, and cached tabs hide theirs (see
            # update_identity).
            self.loaded({"session": {"authenticated": False}})
            self.connect(result[0], previous=current)

        self.app.push_screen(
            CellInput(
                current,
                self.query_one("#settings", Button).region,
                parse,
                "https://app.router.com",
                "Save",
                title="Router URL",
                width=48,
                above=True,
            ),
            chosen,
        )


class HomePage(AccountPage):
    page_title = "Home"


def _tokens(value: object) -> str:
    try:
        count = int(value)
    except (TypeError, ValueError):
        return "—"
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if count >= size:
            return f"{count / size:.1f}{suffix}"
    return str(count)


def _latency(value: object) -> str | None:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return None
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def usage_latency(summary: dict) -> str:
    """Median and tail response time; the API has no tokens-per-second figure."""
    latency = [
        f"{_latency(summary.get(f'{name}_latency_ms'))} {name}"
        for name in ("p50", "p95")
        if _latency(summary.get(f"{name}_latency_ms"))
    ]
    return " · ".join(latency) or "—"
