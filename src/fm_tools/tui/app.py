"""Task navigation for bare ``fm``; workflow behavior stays in the CLI.

See README.md in this directory for execution and verification contracts.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from rich import box
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
from fm_tools.cli.registry import REPOS, Repo, current_platform
from . import logo
from .palette import AMBER, BRICK, CREAM, LILAC, PLUM, SAND
from .runner import Launch, environment, invocation, run_terminal

# Keys match the ``group`` a repository may declare in its fm.json.
GROUPS = {
    "workspace": (
        "Check my workspace",
        "Inspect repository branches, local changes, workspace paths, and health.",
    ),
    "device": (
        "Work with a device",
        "Find a device, connect over SSH, or inspect machine configuration.",
    ),
    "data": (
        "Work with robot data",
        "Find recording, processing, archive, and policy commands.",
    ),
    "robot": (
        "Run a robot or simulation",
        "Find the available robot and simulation workflows. Review targets before running.",
    ),
    "develop": (
        "Develop FM software",
        "Build packages, run the Desktop app, and check designs and diagrams.",
    ),
    "maintain": (
        "Set up or maintain FM",
        "Install, update, reset, and inspect releases. Review changes before running.",
    ),
    "all": (
        "Browse all actions",
        "Find every available action in this workspace.",
    ),
}
# Only these exact argument lists can execute without a review.
REPORTS = {
    "root": ("Workspace location", ("root", "--json")),
    "list": ("List of repos", ("list", "--json")),
    "status": ("Repository status", ("status", "--no-fetch", "--json")),
    "commands": ("Available actions", ("commands", "--json")),
}
LABELS = {
    **{key: value[0] for key, value in REPORTS.items()},
    "doctor": "Run health checks",
    "update": "Update workspace",
    "setup": "Set up workspace",
    "install": "Install repo",
    "reset": "Reset repo",
    "uninstall": "Uninstall repo",
    "release": "Check releases",
    "device": "Manage devices",
    "diagram": "Work with diagrams",
    "run": "Run a custom command",
    "data-refine": "Prepare robot data",
    "agent": "Manage the host agent",
    "archive": "Manage archive services",
    "build": "Build workspace packages",
    "data-annotate": "Annotate robot data",
    "data-archive": "Manage archived data",
    "data-hands": "Track hands in recordings",
    "data-pack": "Build a dataset release pack",
    "data-process": "Process a recording",
    "data-showcase": "Prepare an episode showcase",
    "dataset": "Process recorded episodes",
    "dataset-release": "Inspect dataset releases",
    "demo": "Try the Desktop demo",
    "desktop": "Open Desktop",
    "desktop-check": "Check Desktop",
    "design-audit": "Check design rules",
    "design-review": "Review design changes",
    "episode": "Work with recorded episodes",
    "flash": "Prepare boot media",
    "foxglove": "Connect Foxglove",
    "glove-receiver": "Manage glove receivers",
    "isaac-sim": "Start Isaac Sim",
    "lidar-health": "Check lidar health",
    "lidar-net": "Configure the lidar network",
    "lidar-power": "Set lidar power mode",
    "machine": "View or configure this machine",
    "new-surface": "Create a Desktop view",
    "package-plugin": "Build the team plugin",
    "pkg": "Install a package",
    "policy": "Train and evaluate policies",
    "process": "Manage data processing",
    "rig-health": "Check recording rig health",
    "rig-load": "Check recording rig load",
    "robot": "Manage a robot",
    "setup-add-user": "Add a machine user",
    "setup-backup": "Back up machine data",
    "setup-check": "Check machine setup",
    "setup-onboard": "Set up your account",
    "setup-robot-sudo": "Configure robot service access",
    "sim": "Start a robot simulation",
    "stack": "Manage the robot stack",
    "teleop": "Control a robot arm",
    "ui-audit": "Check the Desktop interface",
    "view-robot": "View a robot model",
}


DESCRIPTIONS = {
    "list": "Select a repo, inspect its status, and choose an action.",
    "status": "See branches and local changes across your repos.",
    "doctor": "Check the workspace and find problems that need attention.",
    "root": "See where your workspace is stored.",
    "commands": "Browse all available actions by name.",
    "install": "Choose a repo and install its tools.",
    "reset": "Choose a repo and reset its local setup.",
    "uninstall": "Choose a repo and remove its installed tools.",
}


# Verbs whose effects the review screen shows as a warning.
QUIET_EFFECTS = ("doctor", "status")


def group(verb: str, declared: str = "") -> str:
    """A repository's declared group wins; otherwise guess from the verb."""
    if declared:
        return declared
    if verb in (*REPORTS, "doctor"):
        return "workspace"
    if verb in ("device", "machine", "agent"):
        return "device"
    if verb.startswith(("data", "archive", "episode", "process", "policy")):
        return "data"
    if verb in (
        "robot",
        "sim",
        "stack",
        "teleop",
        "isaac-sim",
        "view-robot",
        "foxglove",
    ) or verb.startswith(("rig-", "lidar-", "glove-")):
        return "robot"
    if verb in (
        "build",
        "demo",
        "desktop",
        "diagram",
        "new-surface",
        "package-plugin",
        "ui-audit",
    ) or verb.startswith(("design-", "desktop-")):
        return "develop"
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
        return "maintain"
    return "all"


def sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def title(row: dict) -> str:
    return LABELS.get(row["verb"]) or sentence(row["help"]) or row["verb"]


def rank(row: dict, words: list[str], group_label: str) -> int | None:
    """None when a word is missing; lower when the verb or title matches."""
    haystack = f"{title(row)} {row['verb']} {row['repo']} {row['help']} {group_label}"
    if not all(word in haystack.casefold() for word in words):
        return None
    query, verb, name = " ".join(words), row["verb"], title(row).casefold()
    if verb == query:
        return 0
    if verb.startswith(query) or name.startswith(query):
        return 1
    return 2 if query in verb or query in name else 3


def command_text(argv: tuple[str, ...]) -> str:
    return shlex.join(("fm", *argv))


def safe_text(value: object) -> str:
    """Treat manifest, path, and process text as data, never terminal controls."""
    return "".join(c if c.isprintable() or c == "\n" else "?" for c in str(value))


LEVELS = {"fail": BRICK, "warn": AMBER, "pass": SAND}

# Columns each list report shows; other fields stay in the JSON contract.
VIEWS = {
    "status": lambda row: {
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
    },
    "list": lambda row: {
        "repo": row.get("name"),
        "directory": row.get("local_dir"),
        "entry points": ", ".join(row.get("entry_points") or ()),
    },
    "commands": lambda row: {
        "verb": row.get("verb"),
        "repo": row.get("repo"),
        "help": sentence(str(row.get("help") or "")),
    },
    "doctor": lambda row: {
        "level": row.get("level"),
        "repo": row.get("repo"),
        "check": row.get("check"),
    },
}


@dataclass
class Session:
    category: str = ""
    query: str = ""
    selected: int = 0
    arguments: dict[str, str] = field(default_factory=dict)
    repo: str = ""
    repo_action: int = 0
    last: Launch | None = None
    result: tuple[int, str] | None = None


class TaskScreen(Screen):
    BINDINGS = [("escape", "back", "Back")]

    def action_back(self) -> None:
        self.app.pop_screen()


class Repositories(TaskScreen):
    def action_back(self) -> None:
        self.app.session.repo = ""
        super().action_back()

    def compose(self) -> ComposeResult:
        yield Static("Home / List of repos", classes="heading")
        yield Static("Select a repo to see its location and available actions.")
        yield OptionList(
            *[
                Text(
                    f"{repo.name}  ·  {'On this machine' if (repo.checkout(self.app.root) / '.git').exists() else 'Not cloned'}"
                )
                for repo in REPOS
            ],
            id="repos",
        )
        yield Footer()

    def on_mount(self) -> None:
        menu = self.query_one(OptionList)
        menu.highlighted = next(
            (i for i, repo in enumerate(REPOS) if repo.name == self.app.session.repo), 0
        )
        menu.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        repo = REPOS[event.option_index]
        if self.app.session.repo != repo.name:
            self.app.session.repo_action = 0
        self.app.session.repo = repo.name
        self.app.push_screen(RepositoryActions(repo))


class RepositoryActions(TaskScreen):
    def __init__(self, repo: Repo) -> None:
        super().__init__()
        self.repo = repo
        self.actions: list[dict] = []

    def compose(self) -> ComposeResult:
        path = self.repo.checkout(self.app.root)
        yield Static(
            f"List of repos / {self.repo.name}", classes="heading", markup=False
        )
        yield Static(safe_text(path), markup=False)
        yield Static("Choose an action for this repo.")
        self.actions = [
            {"verb": "status", "repo": self.repo.name, "help": "View repo status"}
        ]
        if (path / "install.sh").is_file() and self.repo.applies_to(current_platform()):
            self.actions.extend(
                dict(row, target=self.repo.name)
                for verb in ("install", "reset", "uninstall")
                for row in self.app.rows
                if row["verb"] == verb
            )
        self.actions.extend(
            row
            for row in self.app.rows
            if row["repo"] == self.repo.name and row["kind"] == "manifest"
        )
        if not (path / ".git").exists():
            yield Static(
                "This repo is not cloned. Use Set up workspace to add missing repos."
            )
            self.actions.extend(row for row in self.app.rows if row["verb"] == "setup")
        yield OptionList(
            *[
                Text(
                    "View repo status"
                    if row["verb"] == "status"
                    else safe_text(title(row))
                )
                for row in self.actions
            ],
            id="repo-actions",
        )
        yield Footer()

    def on_mount(self) -> None:
        menu = self.query_one(OptionList)
        menu.highlighted = min(self.app.session.repo_action, len(self.actions) - 1)
        menu.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.app.session.repo_action = event.option_index
        row = self.actions[event.option_index]
        if row["verb"] == "status":
            self.app.push_screen(
                Result(Launch(REPORTS["status"][1]), report=True, repo=self.repo.name)
            )
        else:
            self.app.push_screen(Form(row))


class Form(TaskScreen):
    def __init__(self, row: dict) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        verb = self.row["verb"]
        yield Static(
            safe_text(f"FIRST MOTIVE / {title(self.row)}"),
            classes="heading",
            markup=False,
        )
        with VerticalScroll():
            yield Static(safe_text(sentence(self.row["help"])), markup=False)
            yield Label("Workspace")
            yield Static(safe_text(self.app.root), markup=False)
            if self.row.get("target"):
                yield Label("Repo")
                yield Static(self.row["target"], markup=False)
            if verb == "update":
                yield Static(
                    "Pull cloned repositories and run their update scripts. This changes local checkouts and can use the network.",
                    classes="effects warn",
                )
            else:
                yield Label("Arguments (advanced)")
                yield Input(
                    self.app.session.arguments.get(self.argument_key, ""),
                    id="arguments",
                    placeholder="Optional installer options"
                    if self.row.get("target")
                    else "Arguments after fm " + verb,
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

    @property
    def argument_key(self) -> str:
        return f"{self.row['verb']}:{self.row.get('target', '')}"

    def on_input_changed(self, event: Input.Changed) -> None:
        self.app.session.arguments[self.argument_key] = event.value
        if event.value:
            self.query_one("#error", Static).update("")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.action_back()
        elif event.button.id == "review":
            verb = self.row["verb"]
            try:
                argv = (
                    (
                        verb,
                        *((self.row["target"],) if self.row.get("target") else ()),
                        *shlex.split(self.query_one(Input).value),
                    )
                    if self.query(Input)
                    else (verb,)
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
            self.app.push_screen(Review(Launch(argv), self.row))


class Review(TaskScreen):
    def __init__(self, launch: Launch, row: dict, report: bool = False) -> None:
        super().__init__()
        self.launch, self.row, self.report = launch, row, report
        self.confirm = not report and row["verb"] != "update"
        self.confirmation = row.get("target") or command_text(launch.argv)

    def compose(self) -> ComposeResult:
        yield Static("FIRST MOTIVE / Review action", classes="heading")
        with VerticalScroll():
            yield Static(safe_text(title(self.row)), classes="heading", markup=False)
            yield Label("Command")
            yield Static(command_text(self.launch.argv), id="preview", markup=False)
            yield Label("Target workspace")
            yield Static(safe_text(self.app.root), markup=False)
            command = self.app.discovery.commands.get(self.row["verb"])
            yield Label("Working directory")
            yield Static(
                safe_text(command.cwd if command else Path.cwd()), markup=False
            )
            yield Label("Owner")
            yield Static(safe_text(self.row["repo"]), markup=False)
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
                classes="effects"
                + ("" if self.row["verb"] in QUIET_EFFECTS else " warn"),
            )
            if self.confirm:
                yield Label(
                    f"Type {self.row['target']} to confirm this repo"
                    if self.row.get("target")
                    else "Confirm the target and effects: type the exact command above"
                )
                yield Input(
                    id="confirmation", placeholder=self.row.get("target", "fm ...")
                )
            yield Static("", id="error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Run action", id="run", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        # Reports cannot change state; everything else starts on Cancel.
        self.query_one("#run" if self.report else "#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_back()
        elif event.button.id == "run":
            if (
                self.confirm
                and self.query_one("#confirmation", Input).value != self.confirmation
            ):
                self.query_one("#error", Static).update(
                    "Confirmation does not match. Check the target and effects."
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
        repo: str = "",
    ) -> None:
        super().__init__()
        self.launch, self.report, self.result = launch, report, result
        self.repo = repo
        self.process: asyncio.subprocess.Process | None = None
        self.running = report
        self.interrupted = False

    def compose(self) -> ComposeResult:
        row = next(
            (row for row in self.app.rows if row["verb"] == self.launch.argv[0]),
            {"verb": self.launch.argv[0], "help": "Action result"},
        )
        yield Static(
            safe_text(f"{self.repo} / {title(row)}" if self.repo else title(row)),
            classes="heading",
            markup=False,
        )
        yield Static("Running…" if self.running else "", id="outcome", markup=False)
        yield RichLog(
            id="output",
            wrap=True,
            markup=False,
            highlight=False,
            auto_scroll=False,
            min_width=20,  # The default 78 overflows an 80-column terminal.
        )
        with Horizontal(classes="buttons"):
            yield Button("Return", id="return", disabled=self.running)
            yield Button("Run again", id="again", disabled=self.running)
            interrupt = Button("Interrupt", id="interrupt")
            interrupt.display = self.running
            yield interrupt
            if self.launch.argv[0] == "status" and not self.repo:
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
            button.disabled = False
        self.query_one("#interrupt", Button).display = False
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
                message += "\nRemote state uses cached refs."
                if not self.repo:
                    message += " Fetch remote refs to refresh them."
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
        rows = (
            [
                row
                for row in data
                if isinstance(row, dict) and row.get("name") == self.repo
            ]
            if self.repo
            else data
        )
        if not rows:
            output.write("No entries.")
            return
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("Report rows must be objects.")
        verb = self.launch.argv[0]
        if verb == "doctor":
            # Put what needs attention first; passes are the long tail.
            order = list(LEVELS)
            rows = sorted(
                rows,
                key=lambda row: (
                    order.index(row.get("level")) if row.get("level") in order else 0
                ),
            )
            counts = Counter(row.get("level") for row in rows)
            output.write(
                Text(
                    " · ".join(f"{counts[level]} {level}" for level in order),
                    style=f"bold {BRICK if counts['fail'] else CREAM}",
                )
            )
        rows = [VIEWS[verb](row) for row in rows] if verb in VIEWS else rows
        columns = list(dict.fromkeys(key for row in rows for key in row))
        table = Table(
            *[safe_text(key) for key in columns],
            box=box.SIMPLE_HEAD,
            header_style=f"bold {LILAC}",
            border_style=SAND,
        )
        for column in table.columns:
            column.overflow = "fold"
        for row in rows:
            table.add_row(
                *[
                    Text(
                        safe_text(
                            json.dumps(row[key], ensure_ascii=False)
                            if isinstance(row.get(key), (list, dict))
                            else row.get(key, "—")
                        ),
                        style=LEVELS.get(row.get(key), "") if key == "level" else "",
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
        elif event.button.id == "again" and self.launch.argv in (
            argv for _, argv in REPORTS.values()
        ):
            self.app.switch_screen(Result(self.launch, report=True, repo=self.repo))
        elif event.button.id == "again":
            row = next(
                row for row in self.app.rows if row["verb"] == self.launch.argv[0]
            )
            if (
                self.app.session.repo
                and self.launch.argv[0] in ("install", "reset", "uninstall")
                and len(self.launch.argv) > 1
            ):
                row = dict(row, target=self.launch.argv[1])
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
    #repos, #repo-actions {{ height: 1fr; border: none; background: {PLUM}; }}
    #search {{ height: 3; margin: 0; }}
    #body {{ height: 1fr; }}
    #tasks {{ width: 1fr; height: 1fr; border: none; background: {PLUM}; text-wrap: nowrap; text-overflow: ellipsis; }}
    #details {{ width: 40%; padding: 0 2; color: {SAND}; }}
    Screen.compact #body {{ layout: vertical; }}
    Screen.compact #details {{ width: 100%; height: 3; padding: 0 1; }}
    #notice {{ height: auto; max-height: 2; color: {AMBER}; }}
    #keys {{ height: 1; color: {SAND}; }}
    Input {{ border: solid {SAND}; background: {PLUM}; color: {CREAM}; }}
    Input:focus {{ border: heavy {LILAC}; }}
    OptionList > .option-list--option-highlighted {{ background: {LILAC}; color: {PLUM}; text-style: bold; }}
    Label {{ margin-top: 1; color: {SAND}; }}
    .effects {{ margin-top: 1; }}
    .warn {{ color: {AMBER}; }}
    Static {{ height: auto; }}
    .buttons {{ height: auto; min-height: 3; margin-top: 1; }}
    Button {{ min-width: 10; height: 1; padding: 0 2; margin-right: 1; border: none; background: {SAND} 15%; color: {CREAM}; }}
    Button:hover {{ background: {SAND} 30%; }}
    Button.-primary {{ background: {LILAC} 40%; color: {CREAM}; }}
    Button:focus, Button.-primary:focus {{ background: {LILAC}; color: {PLUM}; text-style: bold; }}
    Button:disabled {{ background: {PLUM}; color: {SAND} 50%; }}
    #error {{ color: {AMBER}; }}
    RichLog {{ height: 1fr; background: {PLUM}; color: {CREAM}; scrollbar-gutter: stable; }}
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
            placeholder="Find a task",
            id="search",
            # Typing in the menu moves its first character here; keep it.
            select_on_focus=False,
        )
        with Horizontal(id="body"):
            yield OptionList(id="tasks")
            yield Static("", id="details", markup=False)
        yield Static("", id="notice", markup=False)
        yield Static(
            "Arrows Move   Enter Open   / Search   ? Help   Esc Back   Ctrl+Q Quit",
            id="keys",
        )

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
            if self.session.repo:
                repo = next(
                    (repo for repo in REPOS if repo.name == self.session.repo), None
                )
                if repo:
                    self.push_screen(Repositories())
                    self.push_screen(RepositoryActions(repo))
                    if (
                        row
                        and launch.argv[0] in ("install", "reset", "uninstall")
                        and len(launch.argv) > 1
                    ):
                        row = dict(row, target=launch.argv[1])
            if row:
                self.push_screen(Form(row))
            self.push_screen(Result(launch, result=result))

    def resize_home(self) -> None:
        home = self.screen_stack[0]
        home.set_class(self.size.width < 100, "compact")
        art = logo.MARK
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

    def group_of(self, row: dict) -> str:
        command = self.discovery.commands.get(row["verb"])
        return group(row["verb"], command.group if command else "")

    def fill_menu(self) -> None:
        words = self.session.query.casefold().split()
        if words:
            ranked = [
                (score, r)
                for r in self.rows
                if (score := rank(r, words, GROUPS[self.group_of(r)][0])) is not None
            ]
            self.menu_items = [r for _, r in sorted(ranked, key=lambda pair: pair[0])]
        elif self.session.category:
            self.menu_items = [
                r
                for r in self.rows
                if self.session.category == "all"
                or self.group_of(r) == self.session.category
            ]
        else:
            self.menu_items = list(GROUPS)
        menu = self.screen_stack[0].query_one("#tasks", OptionList)
        menu.clear_options()
        menu.add_options(
            [
                Text(
                    safe_text(GROUPS[item][0] if isinstance(item, str) else title(item))
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
            GROUPS[self.session.category][0]
            if self.session.category
            else "What do you want to do?"
        )
        if not self.menu_items:
            self.screen_stack[0].query_one("#details", Static).update(
                "No matching actions. Press Escape to clear the search."
            )
        self.resize_home()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search" and event.value != self.session.query:
            self.session.query, self.session.selected = event.value, 0
            self.fill_menu()

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        if self.screen is not self.screen_stack[0]:
            return
        self.session.selected = event.option_index
        if event.option_index >= len(self.menu_items):
            return
        item = self.menu_items[event.option_index]
        description = (
            GROUPS[item][1]
            if isinstance(item, str)
            else DESCRIPTIONS.get(item["verb"], sentence(item["help"]))
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
        elif item["verb"] == "list":
            self.push_screen(Repositories())
        elif item["verb"] == "commands":
            self.session.category, self.session.query, self.session.selected = (
                "all",
                "",
                0,
            )
            self.screen_stack[0].query_one("#search", Input).value = ""
            self.fill_menu()
        elif item["verb"] in ("install", "reset", "uninstall"):
            self.push_screen(Repositories())
        elif item["verb"] in REPORTS:
            self.push_screen(Result(Launch(REPORTS[item["verb"]][1]), report=True))
        elif item["verb"] == "doctor":
            # Read-only, so there are no arguments to ask for; review is enough.
            self.push_screen(
                Review(Launch(("doctor", "--no-fetch", "--json")), item, report=True)
            )
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
