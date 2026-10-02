"""Task navigation for bare ``fm``; workflow behavior stays in the CLI.

See README.md in this directory for execution and verification contracts.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
from dataclasses import dataclass, field
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label, OptionList, RichLog, Static

from fm_tools.cli.broker import refuse_literal_secrets
from fm_tools.cli.commands import BUILTIN_VERBS, catalogue
from fm_tools.cli.exits import from_returncode
from fm_tools.cli.machine import CardError, read_card
from fm_tools.cli.manifest import Discovery, discover
from . import logo
from .palette import AMBER, CREAM, LILAC, PLUM, SAND
from .runner import Launch, environment, invocation, run_terminal

GROUPS = (
    "Check my workspace",
    "Work with a device",
    "Work with robot data",
    "Run a robot or simulation",
    "Set up or maintain FM",
    "Browse all commands",
)
GROUP_HELP = (
    "Inspect repository branches, local changes, workspace paths, and health.",
    "Find a device, connect over SSH, or inspect machine configuration.",
    "Find recording, processing, archive, and policy commands.",
    "Find the available robot and simulation workflows. Review targets before running.",
    "Install, update, reset, and inspect releases. Review changes before running.",
    "Every command available in this workspace, including new repository commands.",
)
# Only these exact argument lists can execute without a review.
REPORTS = {
    "root": ("Workspace root", ("root", "--json")),
    "list": ("Repositories", ("list", "--json")),
    "status": ("Repository status", ("status", "--no-fetch", "--json")),
    "commands": ("Command catalogue", ("commands", "--json")),
}
LABELS = {
    **{key: value[0] for key, value in REPORTS.items()},
    "doctor": "Run health checks",
    "update": "Update workspace",
}


def group(verb: str) -> str:
    if verb in (*REPORTS, "doctor"):
        return GROUPS[0]
    if verb in ("device", "machine", "agent"):
        return GROUPS[1]
    if verb.startswith(("data", "archive", "episode", "process", "policy")):
        return GROUPS[2]
    if verb in (
        "robot",
        "sim",
        "stack",
        "teleop",
        "isaac-sim",
        "view-robot",
    ) or verb.startswith(("rig-", "lidar-", "glove-")):
        return GROUPS[3]
    if verb in (
        "setup",
        "install",
        "reset",
        "uninstall",
        "update",
        "release",
        "pkg",
        "flash",
    ) or verb.startswith("setup-"):
        return GROUPS[4]
    return GROUPS[5]


def command_text(argv: tuple[str, ...]) -> str:
    return shlex.join(("fm", *argv))


def safe_text(value: object) -> str:
    """Treat manifest, path, and process text as data, never terminal controls."""
    return "".join(c if c.isprintable() or c == "\n" else "?" for c in str(value))


@dataclass
class Session:
    category: str = ""
    query: str = ""
    selected: int = 0
    arguments: dict[str, str] = field(default_factory=dict)
    last: Launch | None = None
    result: tuple[int, str] | None = None


class TaskScreen(Screen):
    BINDINGS = [("escape", "back", "Back")]

    def action_back(self) -> None:
        self.app.pop_screen()


class Form(TaskScreen):
    def __init__(self, row: dict) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        verb = self.row["verb"]
        yield Static(
            f"FIRST MOTIVE / {LABELS.get(verb, verb)}", classes="heading", markup=False
        )
        with VerticalScroll():
            yield Static(safe_text(self.row["help"]), markup=False)
            yield Label("Workspace")
            yield Static(safe_text(self.app.root), markup=False)
            if verb == "update":
                yield Static(
                    "Pull cloned repositories and run their update scripts. This changes local checkouts and can use the network."
                )
            elif verb == "doctor":
                yield Static(
                    "Check health without fetching Git refs. Declared preflights can still use the network."
                )
            else:
                yield Label("Arguments (advanced)")
                yield Input(
                    self.app.session.arguments.get(verb, ""),
                    id="arguments",
                    placeholder="Arguments after fm " + verb,
                )
                yield Static(
                    "Use quotes for spaces. No shell expansion or pipes. Do not enter credentials. Targets and effects are owned by this command."
                )
            yield Static("", id="error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Back", id="back")
                yield Button("Review action", id="review", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        if self.query(Input):
            self.query_one(Input).focus()
        else:
            self.query_one("#back", Button).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.app.session.arguments[self.row["verb"]] = event.value
        if event.value:
            self.query_one("#error", Static).update("")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back()
        elif event.button.id == "review":
            verb = self.row["verb"]
            try:
                argv = (
                    (verb, *shlex.split(self.query_one(Input).value))
                    if self.query(Input)
                    else (
                        (verb, "--no-fetch", "--json") if verb == "doctor" else (verb,)
                    )
                )
            except ValueError:
                self.query_one("#error", Static).update(
                    "Arguments: close each quote before you continue."
                )
                return
            refusal = refuse_literal_secrets(argv)
            if refusal:
                self.query_one(Input).value = ""
                self.query_one("#error", Static).update(refusal)
                return
            if any(not char.isprintable() for arg in argv for char in arg):
                self.query_one("#error", Static).update(
                    "Arguments must not contain control characters."
                )
                return
            self.app.push_screen(
                Review(Launch(argv), self.row, report=verb == "doctor")
            )


class Review(TaskScreen):
    def __init__(self, launch: Launch, row: dict, report: bool = False) -> None:
        super().__init__()
        self.launch, self.row, self.report = launch, row, report
        self.confirm = not report and row["verb"] != "update"

    def compose(self) -> ComposeResult:
        yield Static("FIRST MOTIVE / Review action", classes="heading")
        with VerticalScroll():
            yield Label("Command")
            yield Static(command_text(self.launch.argv), id="preview", markup=False)
            yield Label("Target workspace")
            yield Static(safe_text(self.app.root), markup=False)
            command = self.app.discovery.commands.get(self.row["verb"])
            yield Label("Working directory")
            yield Static(
                safe_text(command.cwd if command else Path.cwd()), markup=False
            )
            yield Static("Owner: " + safe_text(self.row["repo"]), markup=False)
            effects = {
                "update": "Pull repositories and run update scripts. Local files and installed components can change.",
                "doctor": "Run health checks. Declared preflights may contact network services.",
                "status": "Fetch Git refs from remote repositories, then report status. A failed fetch can leave cached remote state.",
                "reset": "Reset the named repository through its installer. Local state can be removed.",
                "uninstall": "Uninstall the named repository. Installed components and local state can be removed.",
                "release": "Inspect or cut a release, depending on the arguments. A cut can publish a tag.",
            }
            yield Static(
                effects.get(
                    self.row["verb"],
                    "This command can change files, services, remote data, or robot state. Verify the target and arguments with the command owner. Existing safety and human approval checks still apply.",
                ),
                markup=False,
            )
            if self.confirm:
                yield Label(
                    "Confirm the target and effects: type the exact command above"
                )
                yield Input(id="confirmation", placeholder="fm ...")
            yield Static("", id="error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Run action", id="run", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_back()
        elif event.button.id == "run":
            if self.confirm and self.query_one(
                "#confirmation", Input
            ).value != command_text(self.launch.argv):
                self.query_one("#error", Static).update(
                    "Confirmation does not match the command. Check its target and arguments."
                )
                self.query_one(Input).focus()
                return
            if self.report:
                self.app.pop_screen()
                self.app.push_screen(Result(self.launch, report=True))
            else:
                self.app.session.last = self.launch
                self.app.exit(self.launch)


class Result(TaskScreen):
    def __init__(
        self,
        launch: Launch,
        report: bool = False,
        result: tuple[int, str] | None = None,
    ) -> None:
        super().__init__()
        self.launch, self.report, self.result = launch, report, result
        self.process: asyncio.subprocess.Process | None = None
        self.running = report
        self.interrupted = False

    def compose(self) -> ComposeResult:
        yield Static(command_text(self.launch.argv), classes="heading", markup=False)
        yield Static("Running…" if self.running else "", id="outcome", markup=False)
        yield RichLog(id="output", wrap=True, markup=False, highlight=False)
        with Horizontal(classes="buttons"):
            yield Button("Return", id="return", disabled=self.running)
            yield Button("Run again", id="again", disabled=self.running)
            yield Button("Interrupt", id="interrupt", disabled=not self.running)
            if self.launch.argv[0] == "status":
                yield Button("Fetch remote refs", id="fetch", disabled=self.running)
        yield Footer()

    def on_mount(self) -> None:
        if self.report:
            self.load_report()
        elif self.result:
            self.finish(*self.result)

    def finish(self, code: int, message: str) -> None:
        self.running = False
        outcome = (
            "Completed" if code == 0 else "Interrupted" if code == 130 else "Failed"
        )
        self.query_one("#outcome", Static).update(f"{outcome} · exit {code}")
        self.query_one(RichLog).write(Text(safe_text(message)))
        for button in self.query(Button):
            button.disabled = button.id == "interrupt"
        self.query_one("#again", Button).disabled = not any(
            row["verb"] == self.launch.argv[0] for row in self.app.rows
        )
        self.query_one("#return", Button).focus()

    @work(exclusive=True)
    async def load_report(self) -> None:
        try:
            self.process = await asyncio.create_subprocess_exec(
                *invocation(self.launch.argv),
                env=environment(self.app.root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            if self.interrupted:
                self.interrupt()
            stdout, stderr = await self.process.communicate()
            code = 130 if self.interrupted else from_returncode(self.process.returncode)
            if stdout and not self.interrupted:
                payload = json.loads(stdout)
                if (
                    not isinstance(payload, dict)
                    or payload.get("schema_version") != 1
                    or payload.get("verb") != self.launch.argv[0]
                    or not isinstance(payload.get("data"), (dict, list))
                ):
                    raise ValueError(
                        "Unsupported report contract. Use the direct CLI to inspect this report."
                    )
                self.render_data(payload["data"])
            elif not stderr and not self.interrupted:
                raise ValueError("The command returned no report.")
            message = stderr.decode(errors="replace")
            if self.launch.argv[0] == "status":
                message += "\nRemote state uses cached refs. Fetch remote refs to refresh them."
            self.finish(code, message)
        except (OSError, ValueError) as exc:
            self.finish(3, str(exc))
        finally:
            if self.process and self.process.returncode is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    await asyncio.wait_for(self.process.wait(), 2)
                except asyncio.TimeoutError:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
                except ProcessLookupError:
                    await self.process.wait()

    def render_data(self, data: dict | list) -> None:
        output = self.query_one(RichLog)
        if isinstance(data, dict):
            for key, value in data.items():
                output.write(Text(f"{safe_text(key)}: {safe_text(value)}"))
            return
        rows = data
        if not rows:
            output.write("No entries.")
            return
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("Report rows must be objects.")
        if self.launch.argv[0] == "status":
            rows = [
                {
                    "repo": row.get("name"),
                    "branch": row.get("branch") or "—",
                    "state": "not cloned"
                    if not row.get("cloned")
                    else "dirty"
                    if row.get("dirty")
                    else "clean",
                    "remote": "unknown"
                    if row.get("ahead") is None
                    else f"+{row['ahead']}/-{row.get('behind')}",
                }
                for row in rows
            ]
        if self.size.width < 100 or self.launch.argv[0] != "status":
            for row in rows:
                for key, value in row.items():
                    output.write(Text(f"{safe_text(key)}: {safe_text(value)}"))
                output.write("")
            return
        columns = list(dict.fromkeys(key for row in rows for key in row))
        table = Table(*[safe_text(key) for key in columns], expand=False)
        for row in rows:
            table.add_row(
                *[
                    Text(
                        safe_text(
                            json.dumps(row[key], ensure_ascii=False)
                            if isinstance(row.get(key), (list, dict))
                            else row.get(key, "—")
                        )
                    )
                    for key in columns
                ]
            )
        output.write(table)

    def interrupt(self) -> None:
        self.interrupted = True
        if self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        self.query_one("#outcome", Static).update(
            "Interruption requested. Waiting for the command to stop…"
        )

    def action_back(self) -> None:
        if not self.running:
            super().action_back()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "return":
            self.action_back()
        elif event.button.id == "interrupt":
            self.interrupt()
        elif event.button.id == "fetch":
            row = next(row for row in self.app.rows if row["verb"] == "status")
            self.app.push_screen(Review(Launch(("status", "--json")), row, report=True))
        elif event.button.id == "again":
            row = next(
                row for row in self.app.rows if row["verb"] == self.launch.argv[0]
            )
            self.app.push_screen(Review(self.launch, row, report=self.report))


class FmApp(App[Launch]):
    TITLE = "First Motive"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("ctrl+q", "leave", "Quit"),
        ("ctrl+c", "interrupt", "Interrupt"),
        ("escape", "back", "Back"),
    ]
    CSS = f"""
    Screen {{ background: {PLUM}; color: {CREAM}; padding: 0 2; }}
    #identity {{ height: 1; color: {SAND}; }}
    #logo {{ height: auto; color: {LILAC}; margin: 1 0; }}
    .heading {{ height: auto; text-style: bold; margin: 1 0; }}
    #search {{ height: 3; margin: 0; }}
    #body {{ height: 1fr; }}
    #tasks {{ width: 1fr; height: 1fr; border: none; background: {PLUM}; }}
    #details {{ width: 40%; padding: 1 2; color: {SAND}; }}
    Screen.compact #body {{ layout: vertical; }}
    Screen.compact #details {{ width: 100%; height: 3; padding: 0 1; }}
    #notice {{ height: auto; max-height: 2; color: {AMBER}; }}
    #keys {{ height: 1; color: {SAND}; }}
    Input {{ border: solid {SAND}; background: {PLUM}; color: {CREAM}; }}
    Input:focus {{ border: heavy {LILAC}; }}
    OptionList > .option-list--option-highlighted {{ background: {LILAC}; color: {PLUM}; text-style: bold; }}
    Label {{ margin-top: 1; color: {SAND}; }}
    Static {{ height: auto; }}
    .buttons {{ height: auto; min-height: 3; margin-top: 1; }}
    Button {{ min-width: 10; margin-right: 1; }}
    Button.-primary {{ background: {LILAC}; color: {PLUM}; }}
    Button:focus {{ text-style: bold reverse; }}
    #error {{ color: {AMBER}; }}
    RichLog {{ height: 1fr; background: {PLUM}; color: {CREAM}; }}
    Footer {{ background: {PLUM}; }}
    Footer > .footer--key, FooterKey > .footer-key--key {{ background: {PLUM}; color: {LILAC}; }}
    Footer > .footer--description, FooterKey > .footer-key--description {{ background: {PLUM}; color: {SAND}; }}
    * {{ scrollbar-color: {LILAC}; scrollbar-background: {PLUM}; scrollbar-color-hover: {SAND}; scrollbar-color-active: {CREAM}; }}
    """

    def __init__(
        self, root: Path, discovery: Discovery, session: Session | None = None
    ) -> None:
        super().__init__()
        self.root, self.discovery = root, discovery
        self.session = session or Session()
        self.rows = catalogue(discovery, root)
        self.menu_items: list[dict | str] = []
        self.ascii = os.environ.get("FM_TUI_ASCII") == "1"

    def compose(self) -> ComposeResult:
        try:
            card = read_card()
            identity = (
                f"{card.name} · {card.role}"
                if card
                else "Machine identity not configured"
            )
        except CardError as exc:
            identity = str(exc)
        yield Static(
            safe_text(f"{identity} / {self.root}"), id="identity", markup=False
        )
        yield Static("", id="logo", markup=False)
        yield Static("What do you want to do?", id="heading", classes="heading")
        yield Input(
            self.session.query,
            placeholder="Search tasks or commands...  / Search",
            id="search",
        )
        with Horizontal(id="body"):
            yield OptionList(id="tasks")
            yield Static("", id="details", markup=False)
        yield Static("", id="notice", markup=False)
        yield Static("Arrows Move   Enter Open   / Search   ? Help", id="keys")
        yield Footer()

    def on_mount(self) -> None:
        self.resize_home()
        self.fill_menu()
        self.screen_stack[0].query_one("#tasks", OptionList).focus()
        self.screen_stack[0].query_one("#notice").display = bool(
            self.discovery.problems
        )
        if self.discovery.problems:
            self.screen_stack[0].query_one("#notice", Static).update(
                "Command discovery needs attention: "
                + "; ".join(safe_text(p.detail) for p in self.discovery.problems)
            )
        if self.session.result and self.session.last:
            launch, result = self.session.last, self.session.result
            self.session.result = None
            row = next((r for r in self.rows if r["verb"] == launch.argv[0]), None)
            if row:
                self.push_screen(Form(row))
            self.push_screen(Result(launch, result=result))

    def resize_home(self) -> None:
        home = self.screen_stack[0]
        home.set_class(self.size.width < 100, "compact")
        art = (
            logo.WIDE
            if self.size.height >= 42 and self.size.width >= 100
            else logo.COMPACT
        )
        if self.ascii:
            art = art.translate(str.maketrans({"▀": "^", "▄": "_", "█": "#"}))
        home.query_one("#logo", Static).update(
            art if self.size.height >= 24 else "FIRST MOTIVE"
        )
        home.query_one("#logo").display = not bool(
            self.session.category or self.session.query
        )

    def on_resize(self, event: events.Resize) -> None:
        if self.is_mounted:
            self.resize_home()

    def fill_menu(self) -> None:
        query = self.session.query.casefold().strip()
        if query:
            self.menu_items = [
                r
                for r in self.rows
                if all(
                    word
                    in f"{LABELS.get(r['verb'], '')} {r['verb']} {r['repo']} {r['help']} {group(r['verb'])}".casefold()
                    for word in query.split()
                )
            ]
        elif self.session.category:
            self.menu_items = [
                r
                for r in self.rows
                if self.session.category == GROUPS[-1]
                or group(r["verb"]) == self.session.category
            ]
        else:
            self.menu_items = list(GROUPS)
        menu = self.screen_stack[0].query_one("#tasks", OptionList)
        menu.clear_options()
        menu.add_options(
            [
                Text(
                    safe_text(
                        item
                        if isinstance(item, str)
                        else f"{LABELS.get(item['verb'], item['verb'])}  /  fm {item['verb']}"
                    )
                )
                for item in self.menu_items
            ]
        )
        menu.highlighted = (
            min(self.session.selected, len(self.menu_items) - 1)
            if self.menu_items
            else None
        )
        self.screen_stack[0].query_one("#heading", Static).update(
            self.session.category or "What do you want to do?"
        )
        if not self.menu_items:
            self.screen_stack[0].query_one("#details", Static).update(
                "No matching commands. Escape clears search. Browse all commands for setup options."
            )
        self.resize_home()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search" and event.value != self.session.query:
            self.session.query, self.session.selected = event.value, 0
            self.fill_menu()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        self.session.selected = event.option_index
        if event.option_index >= len(self.menu_items):
            return
        item = self.menu_items[event.option_index]
        description = (
            GROUP_HELP[GROUPS.index(item)]
            if isinstance(item, str)
            else f"{item['help']}\nOwner: {item['repo']}\n{command_text(REPORTS[item['verb']][1] if item['verb'] in REPORTS else (item['verb'],))}"
        )
        self.screen_stack[0].query_one("#details", Static).update(
            safe_text(description)
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if self.screen is self.screen_stack[0]:
            self.open_item(event.option_index)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if (
            event.input.id == "search"
            and self.menu_items
            and self.screen is self.screen_stack[0]
        ):
            self.open_item(
                self.screen_stack[0].query_one("#tasks", OptionList).highlighted or 0
            )

    def open_item(self, index: int) -> None:
        item = self.menu_items[index]
        if isinstance(item, str):
            self.session.category, self.session.selected = item, 0
            self.fill_menu()
        elif item["verb"] in REPORTS:
            self.push_screen(Result(Launch(REPORTS[item["verb"]][1]), report=True))
        else:
            self.push_screen(Form(item))

    async def on_key(self, event: events.Key) -> None:
        if self.screen is not self.screen_stack[0]:
            if event.key == "question_mark" and not isinstance(self.focused, Input):
                self.notify(
                    "Tab moves between fields. Escape goes back. Review the target before Run."
                )
            return
        search = self.screen_stack[0].query_one("#search", Input)
        if event.key == "down" and search.has_focus:
            self.screen_stack[0].query_one("#tasks", OptionList).focus()
            event.stop()
        elif event.key in ("slash", "question_mark") and not isinstance(
            self.focused, Input
        ):
            event.stop()
            if event.key == "slash":
                search.focus()
            else:
                self.notify(
                    "Type to search. Arrows move. Enter opens. Escape goes back. Ctrl+Q quits."
                )
        elif event.is_printable and not isinstance(self.focused, Input):
            search.value += event.character or ""
            search.focus()
            search.cursor_position = len(search.value)
            event.stop()

    def action_back(self) -> None:
        if self.screen is not self.screen_stack[0]:
            self.screen.action_back()
        elif self.session.query:
            self.screen_stack[0].query_one("#search", Input).value = ""
            self.screen_stack[0].query_one("#tasks", OptionList).focus()
        elif self.session.category:
            self.session.category, self.session.selected = "", 0
            self.fill_menu()

    def action_leave(self) -> None:
        if isinstance(self.screen, Result) and self.screen.running:
            self.notify(
                "A report is running. Use Interrupt, then wait for it to stop before quitting."
            )
        else:
            self.exit()

    def action_interrupt(self) -> None:
        if isinstance(self.screen, Result) and self.screen.running:
            self.screen.interrupt()
        else:
            self.exit()


def run_tui(root: Path, discovery: Discovery) -> int:
    session = Session()
    while True:
        app = FmApp(root, discovery, session)
        launch = app.run()
        if launch is None:
            return 0
        session.result = run_terminal(launch, root)
        discovery = discover(root, reserved=BUILTIN_VERBS)
