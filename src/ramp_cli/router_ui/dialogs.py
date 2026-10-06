"""Modal dialogs shared by the Router app's pages."""

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import (
    Horizontal,
    Vertical,
)
from textual.widgets import (
    OptionList,
    Static,
)

from ramp_cli.router_ui.widgets import (
    ARROW_BINDINGS,
    ArrowNavigation,
    Button,
    InlineModal,
)


class Confirm(ArrowNavigation, InlineModal[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), *ARROW_BINDINGS]

    def __init__(self, message: str, accept: str = "Confirm", *, focus_accept=False):
        super().__init__()
        self.message = message
        self.accept = accept
        # Cancel by default; a routine, easily undone change can start on accept.
        self.focus_accept = focus_accept

    def compose(self) -> ComposeResult:
        with Vertical(id="confirmation"):
            yield Static(self.message, markup=False)
            with Horizontal(classes="actions"):
                yield Button("Cancel", id="cancel")
                yield Button(self.accept, id="accept", variant="primary")

    def on_mount(self):
        self.query_one("#accept" if self.focus_accept else "#cancel", Button).focus()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed):
        self.dismiss(event.button.id == "accept")

    def action_cancel(self):
        self.dismiss(False)


class Notice(ArrowNavigation, InlineModal[None]):
    """Information to read before carrying on; one button closes it."""

    BINDINGS = [Binding("escape", "close", "Close"), *ARROW_BINDINGS]

    def __init__(self, title: str, message: str):
        super().__init__()
        self.title_text = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirmation", classes="notice"):
            yield Static(self.title_text, classes="heading", markup=False)
            yield Static(self.message, id="notice-message", markup=False)
            with Horizontal(classes="actions"):
                yield Static("", classes="spacer")
                yield Button("Done", id="done", variant="primary")

    def on_mount(self):
        self.query_one("#done", Button).focus()

    @on(Button.Pressed, "#done")
    def action_close(self):
        self.dismiss(None)


class ChoiceDialog(InlineModal[str | None]):
    """A question with a list of answers: Enter picks, Esc goes back."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, message: str, choices: list[tuple[str, str]]):
        super().__init__()
        self.title_text = title
        self.message = message
        self.choices = choices

    def compose(self) -> ComposeResult:
        with Vertical(id="confirmation", classes="choices"):
            yield Static(self.title_text, classes="heading", markup=False)
            yield Static(self.message, id="choice-message", markup=False)
            yield OptionList(
                *(Text(label) for label, _value in self.choices), id="choice-list"
            )

    def on_mount(self):
        self.query_one("#choice-list", OptionList).focus()

    @on(OptionList.OptionSelected, "#choice-list")
    def chosen(self, event: OptionList.OptionSelected):
        event.stop()
        self.dismiss(self.choices[event.option_index][1])

    def action_cancel(self):
        self.dismiss(None)
