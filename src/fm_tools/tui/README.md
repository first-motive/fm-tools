# Use The FM Terminal Workspace

Run `fm` with no arguments in a terminal. The workspace opens with repo state.
Use the mouse or Tab, arrows, and Enter to select controls. No command or argument
string is required. Direct `fm <command>` use keeps its existing behavior.

## Complete A Workflow

- **Repos** shows branches, local changes, and cached remote state. Select a repo
  for its install, reset, uninstall, and workflow actions.
- **Health** runs the existing checks after confirmation. Failures appear first.
  Select a row to inspect its details.
- **Updates** shows the update plan. Update all clean clones or the selected repo.
  Each repo reports its result as it finishes. Local changes are preserved.
- **Workflows** provides named actions for workspace, device, robot, data, policy,
  development, and maintenance work. Search by task name or group.
- **Activity** shows loading state, elapsed time, output, and the final exit status.
  Inspect structured results, edit inputs, retry, or open the last 20 results.
  Results remain in memory for this session only.

Forms use choice lists, checkboxes, number fields, device discovery, and file
browsing. A value such as a new recording name still needs text input. Required
fields and alternative inputs are checked before confirmation. Files can be
selected from the workspace, home folder, or parent folders. Multiple choices
use a selectable list; free-form multiple values use one value per line.

Every workflow asks for confirmation. Cancel has initial focus. Opening the app
only reads local repo state; remote Git refresh and health checks are explicit
actions. The existing credential broker, robot guards, and data review approvals
still apply.

Output stays inside the app. For a line prompt, use the masked reply field or
Yes/No. Nested terminal menus open in the embedded terminal pane. Click that
pane to send keys and mouse input to the child; Ctrl+G returns to FM controls.
**Terminal input** also opens this pane for an interactive shell. Resizing FM
resizes the child terminal. Stop asks for confirmation, signals the process group,
and waits for shutdown. A local stop is not a robot emergency stop; remote jobs
and services can continue.

Ctrl+W opens workflow search. Ctrl+R focuses the reply field. Ctrl+Q quits after
work stops. Escape returns to repos or closes a dialog. Use a terminal of at
least 80 columns and 24 rows. Redirected input/output and `TERM=dumb` print CLI
help with exit code 2. `NO_COLOR=1` selects monochrome output.

## Keep Logic In Its Owner

The UI collects named values and invokes the same FM interpreter and dispatcher.
It does not reproduce installer, robot, policy, or data handlers. The update
worker calls the public `cli.update.update_repo` service used by `fm update`.

| Module | Responsibility |
| --- | --- |
| `workspace.py` | Navigation, forms, confirmations, reports, and job lifecycle |
| `workflows.py` | Typed controls, parser extraction, and value validation |
| `catalogue.py` | Explicit shell controls and repo manifest controls |
| `schema_export.py` | Export external parser definitions at development time |
| `parser_controls.json` | Shipped controls with source paths and SHA256 hashes |
| `jobs.py` | Stream per-repo update results from the shared update service |
| `terminal.py` | Own the child PTY, forward signals, and apply resize events |
| `terminal_view.py` | Render child terminal state with pyte and forward input |
| `runner.py` | Resolve the same interpreter and reviewed workspace environment |

The old `app.py` launcher is retained for compatibility with its existing internal
callers. Bare `fm` opens `WorkspaceApp`; it never uses the old argument or terminal
handoff screens. The stable `fm-pick` toolkit remains available to other repos.

Reports have a 16 MiB display limit. Retained raw output has a 4 MiB limit per
workflow; the live log holds 10,000 lines. Terminal controls are interpreted as
screen data, not passed to the outer terminal. Child clipboard and title controls
do not change the user's terminal. No shell expansion is applied to form values.

## Declare New Controls

A new mounted command can declare its TUI actions in its existing `fm.json` entry.
The CLI validates this metadata without importing Textual. Invalid metadata is a
health problem; it does not remove an otherwise valid CLI command. Missing
controls are named on the Workflows screen.

```json
{
  "script": "scripts/run/inspect.sh",
  "help": "inspect a recording",
  "tui": [{
    "title": "Inspect a recording",
    "path": [],
    "fields": [{
      "key": "recording",
      "label": "Recording",
      "flag": "--input",
      "kind": "path",
      "required": true
    }],
    "effects": "Read the selected recording."
  }]
}
```

`path` contains fixed subcommand names. Fields use `text`, `number`, `boolean`,
`path`, or `device`. An empty `flag` makes a field positional. `choices` provides
allowed values. `multiple` accepts several values, `repeat` repeats the flag, and
`arity` fixes the number of values. Fields with the same non-empty `exclusive`
value form an alternative-input group; `group_required` requires one selection.
Optional defaults stay with the owning workflow. `description`, `help`, and
`effects` explain purpose, inputs, and changes in normal language.

The shipped catalogue covers the mounted workflow verbs in the development
workspace. Internal transport commands and the raw-command escape are not menu
actions. Remote hardware and service execution still requires their normal
runtime and access. The catalogue is not evidence that those services are online.

Python data and policy forms come from their owners' parsers. From `fm-tools`
in the complete development workspace, refresh the snapshot with:

```sh
uv run --with jsonschema --with PyYAML --with huggingface-hub python - <<'PYTHON'
from pathlib import Path
from fm_tools.tui.schema_export import export
export(Path.cwd().parent, Path("src/fm_tools/tui/parser_controls.json"))
PYTHON
```

Review changed source hashes and controls together when a workflow parser changes.
The shipped snapshot avoids importing robot and data dependencies just to open FM.

## Verify The Interface

```sh
TERM=xterm-256color uv run --extra dev pytest tests/test_pick.py tests/test_manifest.py
TERM=xterm-256color FM_TUI_EVIDENCE_DIR=/tmp/fm-tui-evidence \
  uv run --extra dev pytest tests/test_pick.py::test_fm_terminal_handoff_and_interrupt -q
```

The real-terminal check opens bare FM in a PTY and exercises a child prompt,
a nested FM menu, failure, retry, and confirmed interruption. It writes the full
`terminal-session.ansi` transcript. Other existing E2E checks use real status and
health subprocesses, a disposable Git remote for updates, and literal form values.
They verify that results remain available after another action runs.
