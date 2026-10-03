"""Workflow definitions for the FM ecosystem, with typed, named inputs.

Python workflows use their parser definitions where those live in this wheel.
Shell front doors use explicit controls checked against their owning scripts.
New repo workflows can supply a manifest ``tui`` contract; unknown workflows
are reported as missing controls, never exposed through a raw argument box.
"""

from __future__ import annotations
import json
from pathlib import Path

from fm_tools.cli.manifest import Discovery, valid_tui
from fm_tools.cli.registry import REPOS, ROLES
from fm_tools.cli.machine import ROBOT_KINDS
from .workflows import Action, Field, parser_actions
from .app import LABELS, group


def text(key, label="", flag=None, required=False, **kw):
    return Field(
        key,
        label or key.replace("-", " ").capitalize(),
        "--" + key if flag is None else flag,
        required=required,
        **kw,
    )


def path(key, label="", required=False):
    return text(key, label, required=required, kind="path")


def choice(key, values, label="", flag=None, required=False):
    return text(key, label, flag, required, choices=tuple(values))


def toggle(key, label=""):
    return text(key, label, kind="boolean")


def number(key, label="", required=False):
    return text(key, label, required=required, kind="number")


def action(verb, operation="", fields=(), title="", report=False, effects=""):
    argv = (verb, *operation.split())
    return Action(
        "/".join(argv),
        title
        or (
            LABELS.get(verb, verb.replace("-", " ").capitalize())
            + (" / " + operation.replace("-", " ").capitalize() if operation else "")
        ),
        argv,
        tuple(fields),
        group=group(verb),
        report=report,
        effects=effects,
    )


ROBOT = choice("robot", ("openarm", "so101", "g1_d", "axol"), "Robot model")
BACKEND = choice("backend", ("mock", "mujoco", "gazebo", "isaac", "real"), "Runtime")
HOST = text("host", "Host", kind="device")
INPUT = path("input", "Input file or folder")
OUTPUT = path("output", "Output folder")
CONFIG = path("config", "Configuration file")


def shell_actions() -> list[Action]:
    result = [
        Action(
            "status-fetch",
            "Refresh repo state from remotes",
            ("status", "--json"),
            report=True,
            effects="Fetch Git refs for cloned repos and read their state. Working files stay unchanged.",
        ),
        Action(
            "doctor-fetch",
            "Check health with remote Git state",
            ("doctor", "--json"),
            report=True,
            effects="Fetch Git refs and run declared health checks. These checks can contact services.",
        ),
        Action(
            "list-details",
            "Inspect repository details",
            ("list", "--json"),
            report=True,
        ),
    ]

    def add(verb, ops=("",), fields=(), **kw):
        for op in ops:
            result.append(action(verb, op, fields, **kw))

    add(
        "setup",
        fields=(
            choice("role", ROLES, "Machine role"),
            toggle("dry-run", "Preview only"),
        ),
    )
    add(
        "release",
        fields=(choice("repo", tuple(r.name for r in REPOS), "Repo"),),
        title="Check release readiness",
    )
    for repo in (r for r in REPOS if r.release_script):
        for publish in (False, True):
            verb = "Publish" if publish else "Preview"
            fields = (
                (
                    toggle("minor", "Increase the minor version"),
                    toggle("only-untagged", "Only repos with no release tag"),
                    text(
                        "include", "Include manifest paths", multiple=True, repeat=True
                    ),
                )
                if repo.name == "fm-ros2"
                else ()
            )
            result.append(
                Action(
                    f"release/{repo.name}/{verb.lower()}",
                    f"{verb} release · {repo.name}",
                    (
                        "release",
                        "--repo",
                        repo.name,
                        "--cut",
                        "--",
                        *(("--apply",) if publish else ()),
                    ),
                    fields,
                    group="maintain",
                    effects=(
                        "Create and push release tags from the remote default branch after CI checks. "
                        "The fm-ros2 release can tag every included repo."
                        if publish
                        else "Check CI and show the release plan without creating tags."
                    ),
                )
            )
    add("device", ("list",), title="List devices")
    add(
        "device",
        ("ssh",),
        fields=(text("device", "Device", "", True, kind="device"),),
        title="Connect to a device",
    )
    add(
        "device",
        ("tunnel",),
        fields=(
            text("device", "Device", "", True, kind="device"),
            text("ports", "Local and remote port", "", True),
        ),
        title="Open a device tunnel",
    )
    add(
        "device",
        ("update",),
        fields=(
            text("device", "Device", "", True, kind="device"),
            text("ref", "Version or commit"),
            toggle("no-restart", "Do not restart services"),
        ),
        title="Update a device",
    )
    add(
        "diagram",
        ("list", "render", "check", "watch"),
        fields=(choice("repo", tuple(r.name for r in REPOS), "Repo"),),
    )
    add("agent", ("status", "logs", "restart", "doctor", "chat"))
    result.append(
        Action(
            "agent/logs/errors",
            "Read agent error log",
            ("agent", "logs", "--err"),
            group="device",
        )
    )
    add(
        "archive",
        ("status", "preflight", "reconcile", "install"),
        fields=(
            HOST,
            toggle("storage", "Include storage state"),
            toggle("dry-run", "Preview only"),
        ),
    )
    add(
        "build",
        fields=(
            text("packages-select", "Packages to build"),
            toggle("symlink-install", "Use symbolic links"),
        ),
    )
    add(
        "dataset",
        ("process", "verify", "profile"),
        fields=(
            INPUT,
            OUTPUT,
            CONFIG,
            BACKEND,
            toggle("strict", "Require every quality check"),
        ),
    )
    add("dataset-release", ("status", "list"))
    add(
        "dataset-release",
        ("show", "verify"),
        fields=(
            text("item", "Candidate or pack", "", True),
            toggle("strict", "Run every validator"),
        ),
    )
    add(
        "demo",
        fields=(
            toggle("skip-build", "Use the existing build"),
            path("log", "Log file"),
        ),
    )
    add("desktop", fields=(toggle("source", "Build from source"),))
    add(
        "desktop-check",
        fields=(
            text("filter", "Suite name"),
            toggle("list", "List suites only"),
            toggle("ui", "Include interface checks"),
        ),
    )
    add("design-audit")
    add("design-review", fields=(text("reference", "Compare against", ""),))
    add("episode", ("list", "stop"), fields=(BACKEND,))
    add(
        "episode",
        ("record",),
        fields=(
            number("duration", "Duration in seconds"),
            text("task-id", "Task"),
            text("instruction", "Instruction"),
            path("output-dir", "Recording folder"),
            BACKEND,
        ),
    )
    add(
        "flash",
        fields=(
            choice("role", ("jetson", "workstation"), "Machine role"),
            path("device", "Target disk", True),
            text("name", "Machine name"),
            text("fleet", "Fleet"),
            text("transport", "Transport"),
            text("workload", "Workload"),
            text("user", "User"),
            path("ssh-key", "SSH public key"),
            toggle("no-provision", "Identity only"),
            toggle("dry-run", "Preview only"),
        ),
        effects="Erase the selected removable disk and write boot media. The installer will also request its own erase confirmation. Credentials stay in the existing environment and broker.",
    )
    add(
        "foxglove",
        fields=(
            Field("port", "Port", "-p", "number"),
            Field("temporary", "Use a temporary container", "-t", "boolean"),
        ),
    )
    add("isaac-sim", title="Start Isaac Sim")
    add("lidar-net", fields=(text("interface", "Network interface", "", True),))
    result.append(
        Action(
            "lidar-net/remove",
            "Remove lidar network configuration",
            ("lidar-net", "--remove"),
            group="robot",
            effects="Remove this machine's lidar network configuration.",
        )
    )
    add("machine", ("show", "doctor", "reset"))
    add(
        "machine",
        ("init",),
        fields=(
            text("name", "Machine name"),
            choice(
                "role", ("workstation", "jetson", "trainer", "mac", "robot"), "Role"
            ),
            text("fleet", "Fleet"),
            text("transport", "Transport"),
            text("workload", "Workload"),
            text("robot", "Robot model"),
            path("workspace", "Workspace folder"),
            path("storage-config", "Storage configuration"),
            toggle("dry-run", "Preview only"),
        ),
    )
    add(
        "new-surface",
        fields=(
            text("name", "View name", "", True),
            text("action", "Main action label", required=True),
        ),
    )
    add("package-plugin")
    add("pkg", ("list", "remove"))
    add("pkg", ("add",), fields=(text("package", "Package name", "", True),))
    add("process", ("status", "list"), fields=(HOST,))
    for op in (
        "show",
        "inspect",
        "run",
        "hands",
        "annotate",
        "real-annotate",
        "retry",
        "wait",
    ):
        fields = [
            text("item", "Request ID" if op == "wait" else "Episode", "", True),
            HOST,
        ]
        if op == "run":
            fields += [
                toggle("emit", "Write RLDS output"),
                toggle("reprocess", "Process again"),
                text("target", "Processing target"),
            ]
        if op in ("real-annotate", "retry"):
            fields += [
                text(k)
                for k in (
                    "approved-by",
                    "model",
                    "runtime",
                    "approval-policy",
                    "profile-id",
                    "profile-version",
                    "profile-sha256",
                    "profile-approval-sha256",
                    "request-id",
                )
            ]
        add("process", (op,), fields=fields)
    add(
        "process",
        ("review",),
        fields=(path("request", "Review request file", True), HOST),
    )
    add(
        "process",
        ("cloud-start", "cloud-cancel"),
        fields=(
            HOST,
            choice("lane", ("qwen2.5", "qwen3.5"), "Lane", required=True),
            text("profile-digest", required=True),
            text("request-id"),
            number("run-minutes", "Lease length in minutes"),
        ),
    )
    add("setup-add-user", fields=(text("user", "User name", "", True),))
    add(
        "setup-backup",
        fields=(
            Field("destination", "Destination folder", "", "path", required=True),
            toggle("verify", "Verify an existing backup"),
            toggle("restore", "Restore the backup"),
            toggle("dry-run", "Preview only"),
            toggle("workspace-data", "Use workspace data"),
        ),
    )
    add(
        "setup-check",
        fields=(
            toggle("workstation"),
            toggle("jetson"),
            toggle("trainer"),
            text("only", "Check only these steps"),
            text("skip", "Skip these steps"),
        ),
    )
    add("setup-onboard")
    add(
        "setup-robot-sudo",
        fields=(
            text("user", "User"),
            toggle("dry-run", "Preview only"),
            toggle("remove", "Remove access"),
        ),
    )
    add(
        "sim",
        fields=(
            ROBOT,
            BACKEND,
            text("variant", "Robot variant"),
            text("task-env", "Task environment"),
        ),
    )
    add(
        "stack",
        ("status", "up", "down"),
        fields=(
            ROBOT,
            BACKEND,
            text("variant", "Robot variant"),
            text("task-env", "Task environment"),
        ),
    )
    add(
        "teleop",
        fields=(
            ROBOT,
            BACKEND,
            text("variant", "Robot variant"),
            choice(
                "input",
                ("foxglove", "joy", "spacenav", "vision", "mirror"),
                "Control input",
            ),
        ),
    )
    add("view-robot", fields=(ROBOT,))
    add(
        "ui-audit",
        fields=(
            toggle("static", "Check source only"),
            choice(
                "route", ("all", "home", "archive", "cloud", "robot-data"), "Screen"
            ),
            toggle("skip-build", "Use the existing build"),
            OUTPUT,
        ),
    )
    for verb in ("rig-health", "rig-load", "lidar-health"):
        add(verb, fields=(HOST,))
    add(
        "lidar-power",
        fields=(choice("mode", ("status", "idle", "sample"), "Power mode", ""), HOST),
    )
    add(
        "glove-receiver",
        fields=(
            choice("mode", ("status", "on", "off"), "Receiver state", ""),
            choice("hand", ("left", "right"), "Hand"),
            HOST,
        ),
    )
    return result


def repo_actions(root: Path, discovery: Discovery, name: str) -> list[Action]:
    repo = next((r for r in REPOS if r.name == name), None)
    if repo is None:
        return []
    result = [
        Action(
            "update-repos",
            "Update this repo",
            (name,),
            description=f"Update {name}. Local changes are preserved.",
            effects="Pull this repo with fast-forward only, then run its update steps.",
        )
    ]
    if (repo.checkout(root) / "install.sh").is_file():
        for verb, title in [
            ("install", "Install repo"),
            ("reset", "Reset repo"),
            ("uninstall", "Uninstall repo"),
        ]:
            result.append(
                Action(
                    f"{verb}/{name}",
                    title,
                    (verb, name),
                    description=f"{title}: {name}",
                    effects="Run the repo installer. Installed components and local setup can change.",
                )
            )
    result.extend(
        a
        for a in actions_for(root, discovery)
        if a.argv[0] in discovery.commands
        and discovery.commands[a.argv[0]].repo == name
    )
    return result


def actions_for(root: Path, discovery: Discovery) -> list[Action]:
    from fm_tools.data_refine import build_parser

    snapshot = json.loads(Path(__file__).with_name("parser_controls.json").read_text())
    exported = []
    for row in snapshot["actions"]:
        row = dict(row)
        row["fields"] = tuple(Field(**field) for field in row["fields"])
        row["argv"] = tuple(row["argv"])
        row["title"] = LABELS.get(row["argv"][0], row["argv"][0]) + (
            " / " + row["title"] if len(row["argv"]) > 1 else ""
        )
        if row["argv"][:2] == ("data-showcase", "publish"):
            row["argv"] += ("--reuse-bundle",)
        exported.append(Action(**row))
    result = (
        profile_actions()
        + shell_actions()
        + extended_actions()
        + exported
        + parser_actions(build_parser(), ("data-refine",), "data")
    )
    mounted = {
        "status",
        "doctor",
        "list",
        "setup",
        "release",
        "device",
        "diagram",
        "data-refine",
        "archive",
        *discovery.commands,
    }
    result = [a for a in result if a.argv[0] in mounted]
    # Explicit repo contracts are read as data and never evaluated as code.
    for repo in REPOS:
        path = repo.checkout(root) / "fm.json"
        try:
            entries = json.loads(path.read_text()).get("commands", {})
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(entries, dict):
            continue
        for verb, entry in entries.items():
            if verb not in discovery.commands or not isinstance(entry, dict):
                continue
            if discovery.commands[verb].repo != repo.name or not valid_tui(
                entry.get("tui", [])
            ):
                continue
            for spec in entry.get("tui", []):
                try:
                    fields = tuple(Field(**field) for field in spec.get("fields", []))
                    suffix = spec.get("path", [])
                    if not isinstance(suffix, list) or not all(
                        isinstance(v, str) and v and not v.startswith("-")
                        for v in suffix
                    ):
                        continue
                    result.append(
                        Action(
                            "/".join((verb, *suffix)),
                            str(spec["title"]),
                            (verb, *suffix),
                            fields,
                            str(spec.get("description", "")),
                            discovery.commands[verb].group or group(verb),
                            effects=str(spec.get("effects", "")),
                        )
                    )
                except (TypeError, KeyError, ValueError):
                    continue
    return list({entry.key: entry for entry in result}.values())


def extended_actions() -> list[Action]:
    result = []
    result.append(
        action(
            "robot",
            "host",
            (
                text("name", "Robot name", "", True),
                path("leader-port", "Leader serial port"),
                path("follower-port", "Follower serial port"),
                text("leader-id", "Leader ID"),
                text("follower-id", "Follower ID"),
                path("root", "Recording folder"),
                path("stack-project", "Robot stack folder"),
                toggle("fake", "Use a simulated robot"),
                text("namespace", "Namespace"),
            ),
            title="Host a robot",
            effects="Start this machine's robot agent. Stop the workflow to stop the local agent.",
        )
    )
    result.append(
        action(
            "data-refine",
            "remote",
            (
                text("host", "Processing host", required=True, kind="device"),
                path("request-file", "Request file", required=True),
                number("timeout", "Timeout in seconds"),
            ),
            title="Run a data request on a processing host",
        )
    )
    device = Field("device", "Device", "", "device", required=True)
    for operation in ("status", "up", "down", "stop", "episodes", "mode"):
        fields = [device]
        if operation == "episodes":
            fields.append(text("dataset", "Dataset", required=True))
        if operation == "mode":
            fields.extend(
                (
                    text("mode", "Mode configuration", "", True),
                    text("arg", "Mode settings", multiple=True, repeat=True),
                )
            )
        result.append(
            Action(
                "robot/" + operation,
                "Robot / " + operation.capitalize(),
                ("robot", "{device}", operation),
                tuple(fields),
                group="robot",
                effects="Send this action to the selected robot. The robot applies its own motion, configuration, and operator checks.",
            )
        )
    result.append(action("robot", "list", title="Discover robots"))
    for operation, choices in (
        ("record", ("start", "stop")),
        ("session", ("get", "set", "clear")),
        ("config", ("get", "set", "rollback")),
    ):
        for sub in choices:
            fields = [device]
            if operation == "config" and sub == "set":
                fields.append(text("setting", "Setting and value", "", True))
            if operation == "record" or (operation == "session" and sub == "set"):
                fields.append(text("dataset", "Dataset", required=True))
            if operation == "record":
                fields.extend(
                    (
                        text("task", "Task instruction"),
                        text("episode", "Episode"),
                        text("note", "Note"),
                    )
                )
            result.append(
                Action(
                    f"robot/{operation}/{sub}",
                    f"Robot / {operation.capitalize()} / {sub.capitalize()}",
                    ("robot", "{device}", operation, sub),
                    tuple(fields),
                    group="robot",
                    effects="Send this action to the selected robot. Existing robot guards and human approval rules still apply.",
                )
            )
    result.append(
        action(
            "data-hands",
            "run",
            (
                path("input", "Recording", True),
                path("output", "Output folder", True),
                text("episode-id"),
                path("model", "Hand model"),
                toggle("selfie-view", "Selfie view"),
                *[
                    number(k)
                    for k in (
                        "min-hand-detection-confidence",
                        "min-hand-presence-confidence",
                        "min-tracking-confidence",
                        "min-cutoff",
                        "beta",
                        "d-cutoff",
                    )
                ],
            ),
        )
    )
    result.append(
        action(
            "data-hands",
            "verify",
            (
                Field("set", "Tracking output", "", "path", required=True),
                path("source", "Source recording"),
            ),
        )
    )
    result.append(
        action(
            "device",
            "adopt",
            (
                text("host", "Host", "", True),
                choice("role", ("robot",), "Role", required=True),
                choice(
                    "robot",
                    ROBOT_KINDS,
                    "Robot kind",
                    required=True,
                ),
                text("name", "Fleet name"),
                text("router", "Router endpoint"),
                text("ref", "Version"),
                number("ros-domain", "ROS domain"),
                toggle("dry-run", "Preview only"),
            ),
        )
    )
    location = text("location", "Storage location")
    item = text("item-id", "Item", "", True)
    for op in (
        "locations",
        "list",
        "search",
        "select",
        "refresh",
        "show",
        "files",
        "preview",
        "history",
        "protect",
        "recover",
    ):
        fields = [HOST]
        if op in ("list", "search", "select"):
            fields += [
                text("query", "Search", ""),
                location,
                text("folder"),
                text("collection"),
                text("kind"),
                text("format"),
                text("producer"),
                text("task"),
                text("recorded-from"),
                text("recorded-to"),
                text("copy-state"),
                number("limit"),
                number("offset"),
            ]
        if op in ("show", "files", "preview", "history", "recover"):
            fields.append(item)
        if op in ("refresh", "files", "preview"):
            fields.append(location)
        result.append(action("archive", "library " + op, fields))
    for noun, ops in (
        ("folder", ("create", "rename", "move", "remove")),
        ("item", ("file", "tags")),
        ("collection", ("create", "rename", "add", "remove")),
    ):
        for op in ops:
            fields = [
                HOST,
                number("revision", "Library revision", True),
                text("request-id", "Request identity", required=True),
            ]
            if op != "create":
                fields.append(text("identity", "Item identity", "", True))
            fields.extend(
                (
                    text("name", "Name"),
                    text("parent", "Parent folder"),
                    text("folder", "Folder"),
                    text("item", "Items", multiple=True, repeat=True),
                    text("tag", "Tags", multiple=True, repeat=True),
                    text("reassign-to", "Move remaining items to"),
                )
            )
            result.append(action("archive", f"library {noun} {op}", fields))
    for op in ("plan", "show", "start", "verify", "download"):
        fields = [HOST]
        if op in ("show", "start", "download"):
            fields.append(text("plan", "Plan", "", True))
        if op == "verify":
            fields += [item, location, toggle("full", "Verify every checksum")]
        if op == "plan":
            fields += [
                text("source", "Source location"),
                text("destination", "Destination location"),
                text("item", "Items", multiple=True, repeat=True),
                text("selection", "Saved selection"),
                number("revision", "Library revision"),
            ]
        if op == "download":
            fields += [
                text("coordinator", "Coordinator"),
                path("destination", "Destination folder", True),
            ]
        result.append(action("archive", "copy " + op, fields))
    for op in ("list", "show", "wait", "pause", "resume", "retry", "cancel"):
        fields = [HOST]
        if op != "list":
            fields.append(text("job", "Job", "", True))
        fields.append(number("timeout", "Wait limit in seconds"))
        result.append(action("archive", "jobs " + op, fields))
    return result


def profile_actions() -> list[Action]:
    result = []
    for operation in (
        "list_profiles",
        "inspect_profile",
        "validate_profile",
        "list_drafts",
        "inspect_draft",
        "import_draft",
        "save_draft",
        "save_profile",
        "submit_profile",
        "decide_profile",
        "clone_profile",
        "list_candidates",
        "inspect_candidate",
    ):
        fields = [
            path("profiles-root", "Profiles folder"),
            path("workspace-root", "Workspace folder"),
        ]
        if operation.startswith("inspect_") or operation in (
            "submit_profile",
            "decide_profile",
        ):
            fields.extend(
                (
                    text("profile_id", "Profile", "$profile_id", True),
                    text("profile_version", "Version", "$profile_version", True),
                )
            )
        if operation in (
            "validate_profile",
            "import_draft",
            "save_draft",
            "save_profile",
        ):
            fields.append(
                Field("profile", "Profile file", "$profile", "jsonfile", required=True)
            )
        if operation == "validate_profile":
            fields.append(Field("approval", "Approval file", "$approval", "jsonfile"))
        if operation == "clone_profile":
            fields.extend(
                text(k, k.replace("_", " ").capitalize(), "$" + k, True)
                for k in (
                    "source_profile_id",
                    "source_profile_version",
                    "new_profile_version",
                )
            )
        if operation == "decide_profile":
            fields.extend(
                (
                    choice(
                        "decision",
                        ("approved", "rejected"),
                        "Decision",
                        "$decision",
                        True,
                    ),
                    text("reason", "Reason", "$reason", True),
                    Field(
                        "actor",
                        "Reviewer identity file",
                        "$actor",
                        "jsonfile",
                        required=True,
                    ),
                )
            )
        result.append(
            Action(
                "data-annotate/profiles/" + operation,
                "Task profiles / " + operation.replace("_", " ").capitalize(),
                ("data-annotate", "profiles"),
                tuple(fields),
                group="data",
                request_operation=operation,
            )
        )
    return result
