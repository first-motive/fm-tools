"""The interactive FM workspace: controls, live actions, and retained results."""

from __future__ import annotations

import asyncio
import codecs
from dataclasses import replace
from contextlib import suppress
import uuid
import json
import os
from pathlib import Path
import signal
import sys
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    DirectoryTree,
    Footer,
    Input,
    Label,
    LoadingIndicator,
    OptionList,
    RichLog,
    Select,
    SelectionList,
    Tree,
    Static,
    TextArea,
)

from fm_tools.cli.broker import refuse_literal_secrets
from fm_tools.cli.manifest import Discovery
from fm_tools.cli.registry import REPOS
from .palette import PLUM, LILAC, CREAM, SAND, AMBER
from .runner import environment, invocation
from .workflows import Action, value_arguments
from .app import safe_text
from .terminal_view import TerminalView

SELECT_EMPTY = getattr(Select, "NULL", Select.BLANK)


class FilePicker(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.selected = root

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static("Choose a file or folder", classes="heading")
            with Horizontal(classes="buttons"):
                yield Button("Up", id="picker-up")
                yield Button("Home", id="picker-home")
                yield Button("Workspace", id="picker-workspace")
            yield DirectoryTree(self.root, id="file-tree")
            yield Static(str(self.root), id="chosen-path", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="picker-cancel")
                yield Button("Use selection", id="picker-use", variant="primary")

    def on_directory_tree_file_selected(self, event):
        self.selected = event.path
        self.query_one("#chosen-path", Static).update(safe_text(event.path))

    def on_directory_tree_directory_selected(self, event):
        self.selected = event.path
        self.query_one("#chosen-path", Static).update(safe_text(event.path))

    def on_button_pressed(self, event):
        event.stop()
        name = event.button.id
        if name in ("picker-up", "picker-home", "picker-workspace"):
            tree = self.query_one(DirectoryTree)
            self.selected = (
                Path(tree.path).parent
                if name == "picker-up"
                else Path.home()
                if name == "picker-home"
                else self.root
            )
            tree.path = self.selected
            self.query_one("#chosen-path", Static).update(safe_text(self.selected))
        else:
            self.dismiss(str(self.selected) if name == "picker-use" else None)

    def action_cancel(self):
        self.dismiss(None)


class Inspect(ModalScreen):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, title, data):
        super().__init__()
        self.title_text, self.data = title, data

    def compose(self):
        with Vertical(id="dialog"):
            yield Static(safe_text(self.title_text), classes="heading", markup=False)
            if isinstance(self.data, (dict, list)) and not (
                isinstance(self.data, dict) and set(self.data) == {"output"}
            ):
                yield Tree("Result", id="inspect-tree")
            yield RichLog(id="inspect-output", wrap=True, markup=False, min_width=20)
            yield Button("Close", id="inspect-close")

    def on_mount(self):
        trees = self.query(Tree)
        if trees:
            tree = trees.first()
            tree.root.data = self.data
            self.add_children(tree.root, self.data)
            tree.root.expand()
            tree.focus()
            self.query_one("#inspect-output").styles.height = 8
        else:
            data = (
                self.data.get("output", self.data)
                if isinstance(self.data, dict)
                else self.data
            )
            self.query_one(RichLog).write(Text(safe_text(data)))

    def add_children(self, node, data):
        entries = (
            data.items()
            if isinstance(data, dict)
            else enumerate(data)
            if isinstance(data, list)
            else ()
        )
        for key, value in entries:
            branch = isinstance(value, (dict, list))
            label = f"{key} · {len(value)} items" if branch else f"{key}: {value}"
            node.add(Text(safe_text(label)), data=value, allow_expand=branch)

    def on_tree_node_expanded(self, event):
        if not event.node.children:
            self.add_children(event.node, event.node.data)

    def on_tree_node_selected(self, event):
        output = self.query_one(RichLog)
        output.clear()
        output.write(Text(safe_text(json.dumps(event.node.data, indent=2))))

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss()

    def action_close(self):
        self.dismiss()


class PastResults(ModalScreen):
    BINDINGS = [("escape", "close", "Close")]

    def compose(self):
        with Vertical(id="dialog"):
            yield Static("Results from this session", classes="heading")
            yield OptionList(
                *(Text(row[0]) for row in reversed(self.app.history)),
                id="history-options",
            )
            yield Button("Close", id="history-close")

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event):
        event.stop()
        summary, data = self.app.history[-1 - event.option_index]
        self.app.push_screen(Inspect(summary, data))

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss()

    def action_close(self):
        self.dismiss()


class DevicePicker(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self):
        with Vertical(id="dialog"):
            yield Static("Choose a device", classes="heading")
            yield Static("Finding devices…", id="devices-status", markup=False)
            yield OptionList(id="device-options")
            yield Button("Cancel", id="device-cancel")

    def on_mount(self):
        self.names = []
        self.load_devices()

    @work(exclusive=True)
    async def load_devices(self):
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *invocation(("device", "list", "--json")),
                env=environment(self.app.root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            output, error = await asyncio.wait_for(process.communicate(), 20)
            if process.returncode:
                raise ValueError(
                    error.decode(errors="replace") or "Device discovery failed."
                )
            rows = json.loads(output)["data"]
            self.names = [row["name"] for row in rows]
            menu = self.query_one("#device-options", OptionList)
            menu.add_options(
                [
                    Text(
                        safe_text(row["name"])
                        + (" · online" if row.get("online") else " · offline")
                    )
                    for row in rows
                ]
            )
            self.query_one("#devices-status", Static).update(
                "Select a device."
                if rows
                else "No devices found. You can enter an existing SSH alias in the form."
            )
            menu.focus()
        except (OSError, ValueError, KeyError, asyncio.TimeoutError) as exc:
            self.query_one("#devices-status", Static).update(
                safe_text(str(exc) or "Device discovery timed out.")
            )
        finally:
            if process and process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                await process.wait()

    def on_option_list_option_selected(self, event):
        event.stop()
        self.dismiss(self.names[event.option_index])

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss(None)

    def action_cancel(self):
        self.dismiss(None)


class Confirm(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, action: Action, argv: tuple[str, ...], summary: str):
        super().__init__()
        self.action, self.argv, self.summary = action, argv, summary

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(self.action.title, classes="heading", markup=False)
            with VerticalScroll(id="confirm-details"):
                yield Static(
                    safe_text(self.summary), id="confirm-summary", markup=False
                )
            yield Static(
                self.action.effects
                or (
                    "Read the selected information. Network checks can take time."
                    if self.action.report
                    else "Run this workflow with the selected values. Its existing checks and approvals still apply."
                ),
                classes="effects",
                markup=False,
            )
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button("Run workflow", id="confirm-run", variant="primary")

    def on_mount(self):
        self.query_one("#confirm-cancel").focus()

    def action_cancel(self):
        self.dismiss(False)

    def on_button_pressed(self, event):
        event.stop()
        self.dismiss(event.button.id == "confirm-run")


def control_key(key: str) -> str:
    return (
        key
        if key.isascii() and all(c.isalnum() or c in "_-" for c in key)
        else "key-" + key.encode().hex()
    )


class WorkflowForm(ModalScreen):
    BINDINGS = [("escape", "cancel", "Back")]

    def __init__(self, action: Action):
        super().__init__()
        self.action = action

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="form-dialog"):
            yield Static(self.action.title, classes="heading", markup=False)
            with VerticalScroll(id="form-fields"):
                yield Static(safe_text(self.action.description), markup=False)
                saved = self.app.form_values.get(self.action.key, {})
                for field in self.action.fields:
                    default = saved.get(field.key, field.default)
                    if field.kind == "boolean":
                        yield Checkbox(
                            Text(safe_text(field.label)),
                            value=bool(saved.get(field.key, False)),
                            id="field-" + control_key(field.key),
                        )
                        continue
                    yield Label(
                        safe_text(field.label)
                        + (" *" if field.required else " (optional)"),
                        markup=False,
                    )
                    if field.exclusive:
                        peers = [
                            f.label
                            for f in self.action.fields
                            if f.exclusive == field.exclusive
                        ]
                        yield Static(
                            "Choose one: " + " or ".join(peers) + ".",
                            classes="hint",
                            markup=False,
                        )
                    if field.help:
                        yield Static(
                            safe_text(field.help), classes="hint", markup=False
                        )
                    if field.multiple and field.choices:
                        selected = str(default).splitlines()
                        yield SelectionList(
                            *(
                                (v.replace("_", " "), v, v in selected)
                                for v in field.choices
                            ),
                            id="field-" + control_key(field.key),
                        )
                    elif field.multiple:
                        yield TextArea(
                            str(default), id="field-" + control_key(field.key)
                        )
                        yield Static("One value per line.", classes="hint")
                    elif field.choices:
                        yield Select(
                            [
                                (Text(safe_text(v.replace("_", " "))), v)
                                for v in field.choices
                            ],
                            value=default or SELECT_EMPTY,
                            id="field-" + control_key(field.key),
                            prompt="Choose " + field.label.lower(),
                        )
                    else:
                        yield Input(
                            str(default),
                            id="field-" + control_key(field.key),
                            type="number" if field.kind == "number" else "text",
                            placeholder="One value per line"
                            if field.multiple
                            else field.label,
                        )
                        if field.kind == "device":
                            yield Button(
                                "Choose device…", id="device-" + control_key(field.key)
                            )
                        if field.kind in ("path", "jsonfile"):
                            yield Button(
                                "Browse…", id="browse-" + control_key(field.key)
                            )
            yield Static("", id="form-error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Back", id="form-back")
                yield Button("Continue", id="form-continue", variant="primary")

    def action_cancel(self):
        self.dismiss()

    def on_button_pressed(self, event):
        event.stop()
        name = event.button.id or ""
        if name == "form-back":
            self.dismiss()
        elif name.startswith("device-"):
            field = self.query_one("#field-" + name[7:], Input)
            self.app.push_screen(
                DevicePicker(),
                lambda value: setattr(field, "value", value) if value else None,
            )
        elif name.startswith("browse-"):
            field = self.query_one("#field-" + name[7:], Input)
            self.app.push_screen(
                FilePicker(self.app.root),
                lambda path: setattr(field, "value", path) if path else None,
            )
        elif name == "form-continue":
            values = {}
            summary = []
            for field in self.action.fields:
                widget = self.query_one("#field-" + control_key(field.key))
                value = (
                    "\n".join(widget.selected)
                    if isinstance(widget, SelectionList)
                    else widget.text
                    if isinstance(widget, TextArea)
                    else widget.value
                )
                if value is SELECT_EMPTY:
                    value = ""
                values[field.key] = value
                if value:
                    summary.append(f"{field.label}: {value}")
            self.app.form_values[self.action.key] = values
            try:
                argv = value_arguments(self.action, values)
                prepared_action = self.action
                if self.action.request_operation:
                    request = {
                        "contract_version": 1,
                        "operation": self.action.request_operation,
                        "request_id": str(uuid.uuid4()),
                    }
                    for field in self.action.fields:
                        if field.flag.startswith("$") and values.get(field.key):
                            value = values[field.key]
                            if field.kind == "jsonfile":
                                source = Path(str(value)).expanduser()
                                if source.stat().st_size > 1024 * 1024:
                                    raise ValueError(
                                        f"{field.label}: the file is too large."
                                    )
                                value = json.loads(source.read_text())
                            request[field.flag[1:]] = value
                    prepared_action = replace(
                        self.action, request_body=json.dumps(request)
                    )
                refusal = refuse_literal_secrets(argv)
                if refusal:
                    raise ValueError(refusal)
            except (ValueError, OSError) as exc:
                self.query_one("#form-error", Static).update(safe_text(exc))
                return
            self.app.push_screen(
                Confirm(
                    self.action,
                    argv,
                    "\n".join(summary) or "Use the workflow defaults.",
                ),
                lambda confirmed: (
                    self.run_confirmed(argv, prepared_action) if confirmed else None
                ),
            )

    def run_confirmed(self, argv, action):
        self.dismiss()
        self.app.start_action(action, argv)


class WorkspaceApp(App):
    TITLE = "First Motive"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("ctrl+q", "quit_workspace", "Quit"),
        ("ctrl+c", "stop_work", "Stop"),
        ("escape", "home", "Back"),
        ("ctrl+w", "workflows", "Workflows"),
        ("ctrl+r", "reply", "Reply"),
    ]
    CSS = f"""
    Screen {{ background: {PLUM}; color: {CREAM}; }}
    #brand {{ padding: 1 2 0 2; text-style: bold; height: 2; color: {LILAC}; }}
    #workspace-path {{ padding: 0 2; color: {SAND}; height: 1; }}
    #navigation {{ height: 3; padding: 0 2; margin-top: 1; }}
    Button {{ min-width: 10; margin-right: 1; border: none; height: 3; background: {SAND} 10%; color: {CREAM}; }}
    Button:focus {{ background: {SAND} 20%; text-style: underline; }}
    Button.-primary {{ background: {LILAC}; color: {PLUM}; text-style: bold; }}
    Button:disabled {{ color: {SAND} 40%; background: {PLUM}; }}
    #content {{ padding: 0 2; height: 1fr; }}
    .heading {{ text-style: bold; height: auto; margin: 1 0; }}
    #summary {{ height: auto; max-height: 3; color: {SAND}; }}
    #results {{ height: 1fr; margin-top: 1; background: {PLUM}; }}
    DataTable > .datatable--header {{ background: {SAND} 10%; color: {SAND}; }}
    DataTable > .datatable--cursor {{ background: {LILAC}; color: {PLUM}; }}
    #detail {{ height: auto; max-height: 4; margin-top: 1; color: {SAND}; }}
    .buttons {{ height: auto; min-height: 3; margin-top: 1; }}
    #actions {{ height: auto; }}
    #loading {{ height: 1; }}
    #terminal {{ height: 1fr; background: {PLUM}; }}
    #terminal-hint {{ height: 1; color: {SAND}; }}
    #output {{ height: 1fr; background: {PLUM}; border: none; }}
    #reply-area {{ height: auto; }}
    #reply {{ width: 1fr; }}
    #workflows {{ height: 1fr; background: {PLUM}; border: none; }}
    TextArea {{ height: 5; border: solid {SAND}; background: {PLUM}; color: {CREAM}; }}
    #workflow-search {{ height: 3; }}
    Input, Select {{ background: {PLUM}; border: solid {SAND}; color: {CREAM}; }}
    Input:focus, Select:focus {{ border: heavy {LILAC}; }}
    SelectCurrent {{ background: {PLUM}; border: none; }}
    SelectOverlay {{ background: {PLUM}; border: solid {LILAC}; }}
    Checkbox {{ height: 1; padding: 0; border: none; background: {PLUM}; color: {CREAM}; }}
    Checkbox > .toggle--button {{ background: {SAND}; color: {SAND}; }}
    Checkbox.-on > .toggle--button {{ background: {LILAC}; color: {PLUM}; }}
    Checkbox:focus {{ background: {SAND} 10%; }}
    OptionList {{ background: {PLUM}; border: solid {SAND}; }}
    OptionList:focus {{ border: solid {LILAC}; }}
    DataTable > .datatable--odd-row {{ background: {SAND} 5%; color: {CREAM}; }}
    DataTable > .datatable--even-row {{ background: {PLUM}; color: {CREAM}; }}
    Footer {{ background: {PLUM}; color: {SAND}; }}
    FooterKey > .footer-key--key {{ background: {PLUM}; color: {LILAC}; }}
    FooterKey > .footer-key--description {{ background: {PLUM}; color: {SAND}; }}
    OptionList > .option-list--option-highlighted {{ background: {LILAC}; color: {PLUM}; }}
    * {{ scrollbar-color: {LILAC}; scrollbar-background: {PLUM}; scrollbar-color-hover: {SAND}; scrollbar-color-active: {CREAM}; }}
    ModalScreen {{ align: center middle; background: {PLUM} 75%; }}
    #dialog {{ width: 85%; max-width: 90; height: auto; max-height: 95%; padding: 1 2; border: heavy {LILAC}; background: {PLUM}; }}
    #dialog.form-dialog {{ height: 90%; }}
    #form-fields {{ height: 1fr; }}
    #form-fields Button {{ height: 1; margin-top: 0; }}
    #form-fields Label {{ margin-top: 1; }}
    #dialog.form-dialog > .heading {{ margin: 0 0 1 0; }}
    #confirm-details {{ height: auto; max-height: 45vh; }}
    #file-tree {{ height: 55vh; }}
    #inspect-output {{ height: 60vh; }}
    #inspect-tree {{ height: 40vh; background: {PLUM}; }}
    SelectionList {{ height: auto; max-height: 8; border: solid {SAND}; }}
    #device-options, #history-options {{ height: 40vh; }}
    Label {{ margin-top: 1; height: auto; }}
    Static {{ height: auto; }}
    .hint {{ color: {SAND}; }}
    .effects {{ margin-top: 1; color: {AMBER}; }}
    #form-error {{ color: {AMBER}; }}
    Screen.compact #brand {{ padding-top: 0; height: 1; }}
    Screen.compact #navigation {{ margin-top: 0; }}
    Screen.compact #detail {{ max-height: 2; }}
    Screen.compact Button {{ min-width: 8; padding: 0 1; margin-right: 1; }}
    """

    def __init__(self, root: Path, discovery: Discovery):
        super().__init__()
        self.root, self.discovery = root, discovery
        self.page = "repos"
        self.status_rows = []
        self.health_rows = []
        self.update_rows = []
        self.selected_repo = ""
        self.current_rows = []
        self.running = False
        self.process = None
        self.last_action = None
        self.last_argv = ()
        self.last_summary = "No workflows run in this session."
        self.stopping = False
        self.started = 0.0
        self.actions = []
        self.action_scope = []
        self.activity_data = None
        self.form_values = {}
        self.history = []
        self.terminal_manual = False
        self.size_pipe = None

    def compose(self) -> ComposeResult:
        yield Static("FIRST MOTIVE  /  Workspace", id="brand")
        yield Static(safe_text(self.root), id="workspace-path", markup=False)
        with Horizontal(id="navigation"):
            for key, name in [
                ("repos", "Repos"),
                ("health", "Health"),
                ("updates", "Updates"),
                ("workflows", "Workflows"),
                ("activity", "Activity"),
            ]:
                yield Button(name, id="nav-" + key)
        with Vertical(id="content"):
            yield Static("Repositories", id="page-title", classes="heading")
            yield Static("Reading local repo state…", id="summary", markup=False)
            yield LoadingIndicator(id="loading")
            yield DataTable(id="results", cursor_type="row", zebra_stripes=True)
            yield Input(placeholder="Find a workflow", id="workflow-search")
            yield OptionList(id="workflows")
            yield RichLog(
                id="output",
                wrap=True,
                markup=False,
                highlight=False,
                max_lines=10000,
                min_width=20,
            )
            yield TerminalView(id="terminal")
            yield Static(
                "Click the terminal to type. Ctrl+G returns to FM controls.",
                id="terminal-hint",
            )
            yield Static("", id="detail", markup=False)
            with Horizontal(id="actions", classes="buttons"):
                yield Button("Refresh", id="refresh")
                yield Button("Check health", id="run-health")
                yield Button("Update repos", id="run-update", variant="primary")
                yield Button("Repo actions", id="repo-actions")
                yield Button("Update selected", id="update-selected")
                yield Button("Inspect result", id="inspect-result")
                yield Button("Past results", id="past-results")
                yield Button("Edit inputs", id="edit-inputs")
                yield Button("Retry", id="retry")
                yield Button("Stop", id="stop")
                yield Button("Terminal input", id="terminal-input")
            with Horizontal(id="reply-area", classes="buttons"):
                yield Input(
                    id="reply",
                    placeholder="Reply to a prompt from the running workflow",
                    password=True,
                )
                yield Button("Send", id="send-reply")
                yield Button("Yes", id="reply-yes")
                yield Button("No", id="reply-no")
        yield Footer()

    def on_mount(self):
        self.set_interval(1, self.refresh_progress)
        self.resize_layout()
        self.show_page("repos")
        self.start_action(
            Action(
                "status",
                "Read repo state",
                ("status", "--no-fetch", "--json"),
                report=True,
            ),
            ("status", "--no-fetch", "--json"),
        )

    def on_resize(self):
        if self.is_mounted:
            self.resize_layout()
            self.call_after_refresh(self.resize_terminal)

    def refresh_progress(self):
        if self.running:
            self.query_one("#summary", Static).update(
                safe_text(
                    f"{self.last_summary} · {time.monotonic() - self.started:.0f}s elapsed"
                )
            )

    def resize_layout(self):
        self.screen_stack[0].set_class(
            self.size.width < 100 or self.size.height < 28, "compact"
        )

    def show_page(self, page):
        self.page = page
        for key in ("repos", "health", "updates", "workflows", "activity"):
            self.query_one("#nav-" + key, Button).variant = (
                "primary" if key == page else "default"
            )
        self.query_one("#page-title", Static).update(
            {
                "repos": "Repositories",
                "health": "Workspace health",
                "updates": "Update repos",
                "workflows": "Workflows",
                "activity": "Current activity",
            }[page]
        )
        self.query_one("#results").display = page in ("repos", "health", "updates")
        self.query_one("#workflows").display = page == "workflows"
        self.query_one("#workflow-search").display = page == "workflows"
        terminal = self.query_one(TerminalView)
        terminal.display = (
            page == "activity"
            and self.running
            and bool(
                self.terminal_manual
                or (terminal.screen_data and terminal.screen_data.alternate)
            )
        )
        self.query_one("#terminal-hint").display = terminal.display
        self.query_one("#output").display = page == "activity" and not terminal.display
        if terminal.display:
            self.call_after_refresh(self.resize_terminal)
        self.query_one("#detail").display = page in ("repos", "health", "updates")
        self.query_one("#reply-area").display = (
            page == "activity"
            and self.running
            and bool(self.last_action and not self.last_action.report)
        )
        visible = {
            "refresh": page == "repos",
            "run-health": page == "health",
            "run-update": page == "updates",
            "repo-actions": page == "repos",
            "update-selected": page == "updates",
            "inspect-result": page == "activity" and self.activity_data is not None,
            "past-results": page == "activity" and bool(self.history),
            "edit-inputs": page == "activity"
            and bool(self.last_action and self.last_action.fields),
            "retry": page == "activity",
            "stop": self.running,
            "terminal-input": page == "activity"
            and self.running
            and not self.last_action.report
            and not self.last_action.request_body,
        }
        for key, show in visible.items():
            button = self.query_one("#" + key, Button)
            button.display = show
            button.disabled = self.running and key not in (
                "stop",
                "terminal-input",
                "past-results",
            )
        self.query_one("#retry", Button).disabled = (
            self.running or self.last_action is None
        )
        self.query_one("#loading").display = self.running
        if page == "repos":
            self.set_table(
                ("Repo", "Branch", "Local state", "Remote"),
                self.status_rows,
                lambda r: (
                    r["name"],
                    r.get("branch") or "—",
                    "Not cloned"
                    if not r.get("cloned")
                    else "Local changes"
                    if r.get("dirty")
                    else "Clean",
                    "Unknown"
                    if r.get("ahead") is None
                    else f"{r['ahead']} ahead · {r['behind']} behind",
                ),
            )
            self.query_one("#summary", Static).update(
                "Select a repo to inspect it or choose an action. Remote state uses cached refs."
            )
        elif page == "health":
            rows = sorted(
                self.health_rows,
                key=lambda r: {"fail": 0, "warn": 1, "pass": 2}.get(r.get("level"), 3),
            )
            self.set_table(
                ("Result", "Repo", "Check"),
                rows,
                lambda r: (
                    r.get("level", "").capitalize(),
                    r.get("repo", ""),
                    r.get("check", ""),
                ),
            )
            counts = {
                level: sum(r.get("level") == level for r in rows)
                for level in ("fail", "warn", "pass")
            }
            self.query_one("#summary", Static).update(
                " · ".join(f"{n} {level}" for level, n in counts.items())
                if rows
                else "Run health checks to see what needs attention. Declared checks may contact services."
            )
        elif page == "updates":
            if self.update_rows:
                self.set_table(
                    ("Repo", "Result", "Details"),
                    self.update_rows,
                    lambda r: (
                        r["name"],
                        "Failed" if not r["ok"] else r["action"].capitalize(),
                        r["detail"],
                    ),
                )
                self.query_one("#summary", Static).update(self.last_summary)
            else:
                self.set_table(
                    ("Repo", "Update plan"),
                    self.status_rows,
                    lambda r: (
                        r["name"],
                        "Skip: not cloned"
                        if not r.get("cloned")
                        else "Skip: local changes"
                        if r.get("dirty")
                        else "Pull and run repo update steps",
                    ),
                )
                self.query_one("#summary", Static).update(
                    "Review the plan. Local changes are preserved. Only clean clones can update."
                )
        elif page == "activity":
            self.query_one("#summary", Static).update(self.last_summary)
        else:
            from .catalogue import actions_for

            self.action_scope = actions_for(self.root, self.discovery)
            words = self.query_one("#workflow-search", Input).value.casefold().split()
            self.actions = [
                a
                for a in self.action_scope
                if all(word in (a.title + " " + a.group).casefold() for word in words)
            ]
            menu = self.query_one("#workflows", OptionList)
            menu.clear_options()
            menu.add_options([Text(f"{a.title}  ·  {a.group}") for a in self.actions])
            missing = sorted(
                set(self.discovery.commands) - {a.argv[0] for a in self.action_scope}
            )
            self.query_one("#summary", Static).update(
                "Select a workflow. Choose its values, confirm, and view the outcome here."
                + (
                    " Controls are not yet declared for: " + ", ".join(missing)
                    if missing
                    else ""
                )
            )

    def set_table(self, columns, rows, values):
        table = self.query_one("#results", DataTable)
        self.current_rows = rows
        table.clear(columns=True)
        table.add_columns(*columns)
        for index, row in enumerate(rows):
            table.add_row(
                *(Text(safe_text(value)) for value in values(row)), key=str(index)
            )
        if rows:
            index = next(
                (
                    i
                    for i, r in enumerate(rows)
                    if r.get("name", r.get("repo")) == self.selected_repo
                ),
                0,
            )
            table.move_cursor(row=index)
            self.show_detail(index)
        else:
            self.query_one("#detail", Static).update(
                "No results yet." if self.page == "health" else "No repos found."
            )

    def on_data_table_row_highlighted(self, event):
        self.show_detail(event.cursor_row)

    def show_detail(self, index):
        if index >= len(self.current_rows):
            return
        row = self.current_rows[index]
        self.selected_repo = row.get("name", row.get("repo", ""))
        repo = next((r for r in REPOS if r.name == self.selected_repo), None)
        if self.page == "health":
            text = f"{self.selected_repo}\n{row.get('check', '')}"
        else:
            text = f"{self.selected_repo}\n{repo.checkout(self.root) if repo else ''}"
        self.query_one("#detail", Static).update(safe_text(text))

    def on_data_table_row_selected(self, event):
        if self.page == "repos":
            self.open_repo()
        elif event.cursor_row < len(self.current_rows):
            self.push_screen(
                Inspect(self.selected_repo, self.current_rows[event.cursor_row])
            )

    def open_repo(self):
        from .catalogue import repo_actions

        self.actions = repo_actions(self.root, self.discovery, self.selected_repo)
        self.show_page("workflows")
        self.action_scope = repo_actions(self.root, self.discovery, self.selected_repo)
        self.actions = self.action_scope
        self.query_one("#workflow-search", Input).value = ""
        menu = self.query_one("#workflows", OptionList)
        menu.clear_options()
        menu.add_options([Text(a.title) for a in self.actions])
        self.query_one("#page-title", Static).update(
            safe_text(self.selected_repo + " / Actions")
        )
        menu.focus()

    def on_input_changed(self, event):
        if event.input.id == "workflow-search":
            words = event.value.casefold().split()
            self.actions = [
                action
                for action in self.action_scope
                if all(
                    word in (action.title + " " + action.group).casefold()
                    for word in words
                )
            ]
            menu = self.query_one("#workflows", OptionList)
            menu.clear_options()
            menu.add_options(
                [Text(action.title + " · " + action.group) for action in self.actions]
            )

    def on_option_list_option_selected(self, event):
        if event.option_index < len(self.actions):
            self.choose(self.actions[event.option_index])

    def choose(self, action):
        if self.running:
            self.notify(
                "Wait for the current workflow or stop it before starting another."
            )
        elif action.fields:
            self.push_screen(WorkflowForm(action))
        else:
            self.confirm(action, action.argv, action.description or action.title)

    def confirm(self, action, argv, summary):
        self.push_screen(
            Confirm(action, argv, summary),
            lambda accepted: self.start_action(action, argv) if accepted else None,
        )

    def on_button_pressed(self, event):
        name = event.button.id or ""
        if name.startswith("nav-"):
            self.show_page(name[4:])
        elif name == "repo-actions":
            self.open_repo()
        elif name == "refresh":
            action = Action(
                "status",
                "Refresh repo state",
                ("status", "--json"),
                report=True,
                effects="Fetch remote Git refs, then refresh repo status. Local files are not changed.",
            )
            self.confirm(action, action.argv, "Refresh all registered repos.")
        elif name == "run-health":
            action = Action(
                "doctor",
                "Check workspace health",
                ("doctor", "--no-fetch", "--json"),
                report=True,
            )
            self.confirm(
                action,
                action.argv,
                "Run the workspace checks. Declared preflights may contact network services.",
            )
        elif name == "run-update":
            names = tuple(
                r["name"]
                for r in self.status_rows
                if r.get("cloned") and not r.get("dirty")
            )
            if not names:
                self.notify("There are no clean cloned repos to update.")
                return
            action = Action(
                "update-repos",
                "Update repos",
                names,
                effects="Pull each selected repo with fast-forward only, then run its update steps. Local changes are checked again before each update.",
            )
            self.confirm(action, names, "\n".join(names))
        elif name == "update-selected" and self.selected_repo:
            action = Action(
                "update-repos",
                "Update selected repo",
                (self.selected_repo,),
                effects="Pull this repo with fast-forward only, then run its update steps. Local changes are preserved.",
            )
            self.confirm(action, action.argv, self.selected_repo)
        elif name == "past-results":
            self.push_screen(PastResults())
        elif name == "inspect-result":
            self.push_screen(Inspect(self.last_action.title, self.activity_data))
        elif name == "edit-inputs" and self.last_action:
            self.push_screen(WorkflowForm(self.last_action))
        elif name == "retry" and self.last_action:
            self.confirm(
                self.last_action,
                self.last_argv,
                "Run the previous workflow again with the same selected values.",
            )
        elif name == "terminal-input":
            self.terminal_manual = not self.terminal_manual
            self.show_page("activity")
            if self.terminal_manual:
                self.query_one(TerminalView).focus()
        elif name == "stop":
            self.action_stop_work()
        elif name in ("send-reply", "reply-yes", "reply-no"):
            value = (
                self.query_one("#reply", Input).value
                if name == "send-reply"
                else "yes"
                if name == "reply-yes"
                else "no"
            )
            if self.process and self.process.stdin:
                try:
                    self.process.stdin.write((value + "\n").encode())
                except (BrokenPipeError, ConnectionResetError):
                    self.notify("The workflow has closed its input.")
            self.query_one("#reply", Input).value = ""

    def start_action(self, action: Action, argv: tuple[str, ...]):
        if self.running:
            return
        self.running, self.stopping = True, False
        self.terminal_manual = False
        self.query_one(TerminalView).reset_terminal(
            max(20, self.size.width - 4), max(5, self.size.height - 19)
        )
        self.last_action, self.last_argv = action, argv
        self.activity_data = None
        self.started = time.monotonic()
        self.last_summary = action.title + " — running…"
        self.query_one("#output", RichLog).clear()
        if action.key == "update-repos":
            self.update_rows = []
            self.show_page("updates")
        elif action.report and argv[0] in ("status", "doctor"):
            self.show_page("repos" if argv[0] == "status" else "health")
        else:
            self.show_page("activity")
        self.query_one("#summary", Static).update(self.last_summary)
        self.execute(action, argv)

    @work(exclusive=True)
    async def execute(self, action, argv):
        output = bytearray()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        code = 3
        read_size = None
        try:
            if action.key == "update-repos":
                command = [
                    sys.executable,
                    "-m",
                    "fm_tools.tui.jobs",
                    str(self.root),
                    *argv,
                ]
            elif action.report or action.request_body:
                command = invocation(argv)
            else:
                command = [sys.executable, "-m", "fm_tools.tui.terminal", *argv]
            read_size, self.size_pipe = os.pipe()
            self.process = await asyncio.create_subprocess_exec(
                *command,
                env={
                    **environment(self.root),
                    "PYTHONUNBUFFERED": "1",
                    "TERM": "xterm-256color",
                    "FM_TUI_SIZE_FD": str(read_size),
                    "FM_TUI_COLUMNS": str(max(20, self.size.width - 4)),
                    "FM_TUI_ROWS": str(max(5, self.size.height - 19)),
                    "NO_COLOR": "1",
                },
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(read_size,),
            )
            os.close(read_size)
            read_size = None
            if action.request_body:
                self.process.stdin.write(action.request_body.encode())
                await self.process.stdin.drain()
                self.process.stdin.close()
            if self.stopping:
                os.killpg(self.process.pid, signal.SIGINT)
                self.wait_for_stop(self.process)
            if action.key == "update-repos":
                async for line in self.process.stdout:
                    event = json.loads(line)
                    if event["event"] == "progress":
                        self.last_summary = f"Updating {event['name']} · {event['done']} of {event['total']} complete"
                    else:
                        self.update_rows.append(event["row"])
                    if self.page == "updates":
                        self.show_page("updates")
                    self.query_one("#summary", Static).update(
                        safe_text(self.last_summary)
                    )
            else:
                while chunk := await self.process.stdout.read(8192):
                    if action.report:
                        if len(output) + len(chunk) > 16 * 1024 * 1024:
                            raise ValueError("The report exceeds the display limit.")
                        output.extend(chunk)
                    else:
                        decoded = decoder.decode(chunk)
                        terminal = self.query_one(TerminalView)
                        was_alternate = terminal.screen_data.alternate
                        plain = terminal.feed(decoded)
                        if len(output) < 4 * 1024 * 1024:
                            output.extend(plain.encode())
                        if terminal.screen_data.alternate != was_alternate:
                            self.show_page(self.page)
                            if (
                                terminal.screen_data.alternate
                                and self.page == "activity"
                            ):
                                terminal.focus()
                        if plain:
                            self.query_one("#output", RichLog).write(Text(plain))

            code = await self.process.wait()
            if not action.report and output:
                try:
                    self.activity_data = json.loads(output.decode(errors="replace"))
                except ValueError:
                    self.activity_data = {"output": output.decode(errors="replace")}

            if action.report and not self.stopping:
                payload = json.loads(output)
                if (
                    not isinstance(payload, dict)
                    or payload.get("schema_version") != 1
                    or payload.get("verb") != argv[0]
                ):
                    raise ValueError(
                        "The workflow returned an unsupported report format."
                    )
                data = payload.get("data", payload)
                if argv[0] in ("status", "doctor") and (
                    not isinstance(data, list)
                    or not all(isinstance(row, dict) for row in data)
                ):
                    raise ValueError("The report must contain rows of results.")
                self.activity_data = data
                if argv[0] == "status":
                    self.status_rows = data
                elif argv[0] == "doctor":
                    self.health_rows = data
                else:
                    self.query_one("#output", RichLog).write(
                        Text(safe_text(json.dumps(data, indent=2)))
                    )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.query_one("#output", RichLog).write(Text(safe_text(exc)))
            if output:
                self.query_one("#output", RichLog).write(
                    Text(safe_text(output.decode(errors="replace")))
                )
            code = 3
            self.activity_data = {
                "error": str(exc),
                "output": output.decode(errors="replace"),
            }
            self.page = "activity"
        finally:
            if self.process and self.process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    await self.process.wait()
            if read_size is not None:
                os.close(read_size)
            if self.size_pipe is not None:
                os.close(self.size_pipe)
                self.size_pipe = None
            self.running = False
            self.process = None
            outcome = (
                "Stopped"
                if self.stopping
                else "Completed"
                if code == 0
                else "Checks need attention"
                if action.report and argv[0] == "doctor" and code == 1
                else "Failed"
            )
            self.last_summary = (
                f"{outcome} · {action.title} · {time.monotonic() - self.started:.1f}s"
                + (f" · exit {code}" if code else "")
            )
            if action.key == "update-repos":
                self.activity_data = list(self.update_rows)
            self.history.append(
                (
                    self.last_summary,
                    self.activity_data or {"output": output.decode(errors="replace")},
                )
            )
            self.history = self.history[-20:]
            self.show_page(self.page)
            if self.page not in ("repos", "health"):
                self.query_one("#summary", Static).update(self.last_summary)
            elif code not in (0, 1):
                self.query_one("#summary", Static).update(self.last_summary)

    def resize_terminal(self):
        terminal = self.query_one(TerminalView)
        if not terminal.display or not terminal.screen_data:
            return
        columns, rows = max(20, terminal.size.width), max(5, terminal.size.height)
        if (terminal.screen_data.columns, terminal.screen_data.lines) == (
            columns,
            rows,
        ):
            return
        terminal.screen_data.resize(lines=rows, columns=columns)
        if self.size_pipe is not None:
            try:
                os.write(self.size_pipe, f"{columns} {rows}\n".encode())
            except (BrokenPipeError, OSError):
                pass
        terminal.refresh()

    def action_workflows(self):
        self.show_page("workflows")
        self.query_one("#workflow-search", Input).focus()

    def action_reply(self):
        if self.running:
            self.show_page("activity")
            self.query_one("#reply", Input).focus()

    def on_input_submitted(self, event):
        if event.input.id == "reply":
            self.query_one("#send-reply", Button).press()
        elif event.input.id == "workflow-search" and self.actions:
            self.choose(self.actions[0])

    def action_home(self):
        if self.screen is self.screen_stack[0]:
            self.show_page("repos")

    def action_quit_workspace(self):
        if self.running:
            self.notify(
                "A workflow is running. Stop it and wait for it to finish before quitting."
            )
        else:
            self.exit()

    def action_stop_work(self):
        if not self.running:
            return
        action = Action(
            "stop",
            "Stop this workflow",
            (),
            effects="Stop the local process. Remote jobs and services may continue. This is not a robot emergency stop.",
        )
        self.push_screen(
            Confirm(action, (), self.last_action.title),
            lambda accepted: self.stop_process() if accepted else None,
        )

    def stop_process(self):
        self.stopping = True
        self.last_summary = "Stopping… waiting for the workflow to exit."
        self.query_one("#summary", Static).update(self.last_summary)
        if self.process and self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGINT)
                self.wait_for_stop(self.process)
            except ProcessLookupError:
                pass

    @work(group="stop", exclusive=True)
    async def wait_for_stop(self, process):
        for timeout, signum in ((4, signal.SIGTERM), (2, signal.SIGKILL)):
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), timeout)
                return
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signum)
                except ProcessLookupError:
                    return
        await process.wait()


def run_tui(root: Path, discovery: Discovery) -> int:
    WorkspaceApp(root, discovery).run()
    return 0
