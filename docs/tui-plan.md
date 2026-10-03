# Plan The FM Terminal Interface

Status: the interface now uses plain-language action menus. **List of repos**
opens a selectable list, followed by repo actions with a fixed installer target.
The review screen keeps the exact command visible. Search accepts both task
names and CLI names. The original design below is historical; its proposal to
show CLI verbs beside menu rows has been replaced by this navigation.
Guided device and data forms remain a follow-up based on use.

## Start With A Task

Run `fm` in a terminal to open a full-screen interface that asks:
**What do you want to do?** Select a task, choose its target, review the action,
and run it. Return to the same place when the action ends.

Keep `fm <command> [arguments]` available for direct use. Both paths must run
the same command and apply the same checks. The interface should help a new
user complete a task and help an experienced user find it faster.

The visual direction is a First Motive control console: a dark plum surface,
warm text, clear lilac selection, compact rows, and generous space around the
main task. Use restrained detail and precise alignment to make it distinctive.

## Define How FM Opens

| Invocation | Proposed result |
|---|---|
| `fm` with terminal input and output | Open the TUI. |
| `fm` with redirected input or output, or `TERM=dumb` | Print help and exit with usage code 2. Do not wait for input. |
| `fm --help` or `fm --version` | Keep the existing text interface. |
| `fm <command> ...` | Keep command arguments, output, credentials, and exit behavior. |
| A command with `--json` | Keep the existing JSON contract. No TUI output. |

Load the TUI only on the interactive, no-argument path. Opening it must not
fetch Git refs, run health checks, contact a robot, or change machine state.
Show a useful error if the workspace cannot be resolved. Do not silently use
a different workspace.

## Make The Home Screen Useful

This wireframe shows structure. Workspace and machine values are placeholders
read from the current configuration. The logo area represents the terminal
artwork specified below; the placeholder is not the final logo.

```text
  [First Motive terminal logo]                      <machine> · <role>
  <workspace>                                                   Home

  What do you want to do?
  [ Search tasks or commands...                                   ]

  > Check my workspace        Branches, local changes, and health
    Work with a device        Find a device or connect to it
    Work with robot data      Inspect, process, and verify data
    Run a robot or simulation Find the available robot workflows
    Set up or maintain FM     Install, update, and inspect releases
    Browse all commands       Everything available here

  Check my workspace
  Start with repository status. Health checks are a separate action.
  Command: fm status --no-fetch

  ↑↓ Move    Enter Open    / Search    ? Help    Ctrl+Q Quit
```

Use task names as the first level. Show the actual FM verb beside each action
on the next screen. Categories contain only commands that are available in
the current workspace. Keep empty categories visible with a short explanation
and a route to setup help.

The search field matches task labels, command names, repository names, and help
text. Search spans all categories. Typing starts a search when a menu has focus;
`/` focuses the field. An empty result keeps the query and offers **Clear search**.
New manifest commands appear in **Browse all commands** without a tools release.

Keep the first release free of saved favorites and automatic recommendations.
Add them only if observed use shows that search and task groups are insufficient.

## Follow One Action Path

```text
Home -> Task -> Target and options -> Review -> Run -> Result
          ^             ^              |               |
          +-------------+---- Back ----+----- Return ---+
```

For a read-only report with no required input, selection can open the report
directly. For other actions, the review screen shows the target, working
directory, exact command, and known effects. Each form uses explicit labels,
validates required fields, preserves input on Back, and puts errors at the
field that needs correction.

Example: **Check my workspace → Repository status** opens a report from
`fm status --no-fetch --json`. Show each repository's branch and local state.
Explain that remote state uses cached refs. A separate **Refresh from remote**
action uses the fetching path. **Run health checks** explains that declared
preflights can use the network even with `--no-fetch`.

Example: **Set up or maintain FM → Update workspace** shows what `fm update`
will do and which workspace it will use. The user selects **Run update**.
When the command ends, show its exit code and offer **Return to tasks** or
**Run again**. Running again returns through review.

For reset, uninstall, release, robot motion, and other actions with material
effects, require a review that names those effects. Destructive actions use
an explicit confirmation of the target, with Cancel selected first. Keep all
existing command checks and human approval requirements. A TUI confirmation
cannot replace a robot interlock or a data review attestation.

## Keep Navigation Predictable

| Control | Behavior |
|---|---|
| Arrow keys | Move within the current list. |
| Enter | Open the selected item or activate the focused control. |
| Tab / Shift+Tab | Move between controls in a stable order. |
| Escape | Close an overlay, then leave search, then go back one screen. |
| Ctrl+Q | Quit when idle. While work runs, open the action-specific exit choice. |
| Ctrl+C | Request interruption of the running command; when idle, exit. |
| `?` outside a text field | Show the current screen's help and shortcuts. |
| Mouse | Select and scroll; every task must also work with the keyboard. |

At Home, Escape does not unexpectedly quit. Text fields retain printable keys,
including `q` and `?`. Keep the current task, target, and Back path visible.
Restore the previous selection and scroll position after a command completes.

## Use The Existing Terminal Brand

This surface already has a home in `fm_tools.tui`. Use its
[terminal palette](../src/fm_tools/tui/palette.py) as the proposed color source.
The shared FM design guidance also contains a web palette; do not mix its
tokens into this terminal surface. Confirm this terminal choice in visual review.

| Existing token | Value | Proposed use |
|---|---|---|
| `PLUM` | `#3B3443` | Main surface. |
| `CREAM` | `#ECE2CF` | Primary text and headings. |
| `SAND` | `#E7DDC8` | Descriptions and supporting text. |
| `LILAC` | `#B6A5C6` | Focus borders, selected text, and the brand mark. |
| `AMBER` | `#D9B96A` | Warnings, always with a text label. |
| `BRICK` | `#C26B6B` | Errors, always with a text label. |

Use a caret, bold text, and a border to identify focus. Color alone must not
carry status. Keep one main panel and a compact command preview; avoid a box
around every row. Reuse the existing brand header without its ROS-specific
connection status. Machine identity does not prove that a robot is connected.

Use the user's terminal font. Do not require a patched font, emoji, or terminal
graphics. Provide ASCII equivalents for the mark, borders, and status symbols.
Do not show an animated introduction. Use activity indicators only while work
is active; offer a static indicator when motion is disabled.

At 100 columns or wider, place the action description beside the menu. At
80 × 24, place it below the menu and keep actions and the footer reachable.
Below that size, show a compact layout or a resize message without losing form
state. Verify 16-color and monochrome output, including `NO_COLOR`, with visible
focus and readable status labels.

## Show The First Motive Logo On Launch

Every interactive `fm` launch must display a terminal rendering of the actual
First Motive logo above **What do you want to do?** Render it inside the first
TUI frame so that screen initialization does not erase it. Keep the menu usable
immediately, with no timed splash screen or extra key press. Keep the logo on
Home; use a compact brand header on task screens.

Start from the canonical vector artwork in the company media library:
`First Motive/01_Brand/first-motive-mark-dark-on-white.svg`, with
`firstmotive-wordmark-light.svg` as the wordmark reference. Record the selected
source and its hash with the generated artwork. The existing `◢` header symbol
is a placeholder, not a conversion of the logo.

During development, convert the vector silhouette into a small character grid.
Account for terminal cells being taller than they are wide so the mark keeps
its proportions. Use Unicode half-block characters (`▀`, `▄`, `█`) for the
normal version, with lilac artwork on plum and no text below the mark. Preserve the
mark's negative space. Review the result beside the vector original before
accepting it; a generic ASCII font spelling the company name is insufficient.

Prepare and package two fixed sizes: a wide version and a compact version that
fits Home at 80 × 24. Use five rows for the compact artwork to preserve the
mark's shape, keeping the task list and footer reachable by keyboard and scroll.
Store the reviewed character art with the package. Do not convert images,
download assets, or require an image library when FM starts.

Provide an ASCII rendering of the same mark for terminals without suitable
Unicode support, with an explicit override because font support cannot be
reliably detected. In monochrome mode, keep the silhouette and lettering and
remove color. At very small sizes, retain a plain company name until the logo
fits again. Direct CLI commands, help, JSON, and redirected output receive no
logo banner.

The interaction prototype must include both logo sizes and the ASCII version.
Save terminal screenshots and check recognition, proportions, alignment, and
contrast in the actual terminal font. Verify that launch, resize, and return
from a child command neither clip the artwork nor print duplicate banners.

## Handle Real Command Behavior

There are two execution paths in the proposed interface:

1. **Structured reports.** Run known reporting commands with `--json` in a
   child process and render the result. Keep the screen responsive, show loading
   and failure states, and reject unsupported payload versions with a useful
   message. A nonzero report exit can still contain valid findings.
2. **Terminal commands.** Restore normal terminal control before a command runs.
   Let it own input, output, prompts, and signals. Reopen the TUI at the previous
   screen after it exits. This supports SSH, interactive scripts, and existing
   TUIs without embedding a second terminal in the first release.

Use the same FM installation that opened the interface. Pass an argument list
to its entry point; do not run a constructed shell string or call repository
scripts directly. Display a safely quoted command preview. Keep credentials in
the existing broker flow, outside previews and saved output.

Keep one foreground action at a time. Do not promise rollback or label an
action **Cancelled** until its process has stopped. Explain that remote jobs
and services can continue after their launch command exits. A local interrupt
is not an emergency stop for a robot. Process completion and exit status must
be observed before the interface offers another execution.

Do not save command output automatically. Reports can offer an explicit export
when the data is safe to save. The first release needs no background job service,
terminal emulator, or persistent command history.

## Build On What Exists

| Existing part | Planned use |
|---|---|
| [CLI entry point](../src/fm_tools/cli/__init__.py) | Add the interactive no-argument branch; preserve normal dispatch. |
| [Command catalogue](../src/fm_tools/cli/commands.py) | Discover verbs and owners from the same data used by the CLI. |
| [Manifest dispatcher](../src/fm_tools/cli/dispatch.py) | Retain delegation and credential behavior through the CLI entry point. |
| [Picker](../src/fm_tools/tui/pick.py) and [widgets](../src/fm_tools/tui/widgets.py) | Reuse visual conventions and suitable components. Keep `fm-pick` independent. |
| [Theme layer](../src/fm_tools/tui/theme.py) | Check appearance with and without optional `nish-tui`; prevent palette drift. |
| [Project dependencies](../pyproject.toml) | Use installed Textual and Rich. Add no UI framework. |

The command catalogue exposes verbs, owners, help, scripts, kinds, and delegates.
It does **not** describe argument types, subcommands, safety, or valid targets.
Do not generate forms or classify a command as safe from its help text.

Start with a small explicit set of guided actions owned by fm-tools. Other
commands get their description, an argument field for experienced users, a
preview, and terminal execution after review. Parse that field into arguments
without shell expansion; make this limitation visible. Never run an unknown
command merely because its row was selected, including to obtain help.

If repository owners later need guided forms, design a small optional manifest
extension with them. Keep old manifests valid. Do not build a generic workflow
engine before the first guided tasks have been used.

## Deliver In Three Stages

### 1. Review The Interaction

Make a terminal prototype of the launch logo, Home, search, a task form, review,
and result.
Use fixture content clearly marked as sample data. Review the actual terminal
at 80 × 24 and a wide size. Save screenshots for both sizes.

Gate: a new user can find repository status, identify the active workspace,
return Home, and find an unfamiliar command without instruction. Target under
one minute per task. Confirm the terminal palette and the task group labels.

### 2. Ship A Useful First Release

Implement bare `fm` entry, navigation, search, command preview, reports for
workspace root/repositories/status, explicit health checks, and terminal
execution. Include one guided write action: workspace update with review.
All discovered verbs remain accessible through Browse all commands.

Gate: a user completes report → update review → cancel → report, then a safe
test command → result → return. Direct CLI invocations retain their contracts.
The interface opens without network work and accepts input within one second
on a documented reference machine and workspace. Measure cold and warm starts.

### 3. Add Guided Workflows From Use

Add device selection, then the most-used robot or data tasks with their owners.
Validate each command's target rules, preconditions, and effects before adding
a form. Use observed tasks to select the order. Add saved favorites or richer
progress only when there is a demonstrated need.

## Verify Before Release

Extend existing CLI and Textual tests where they fit. Obtain approval before
adding test files or fixtures, as required by repository instructions. Use
terminal E2E checks for terminal ownership, interruption, and return behavior.

| Scenario | Required evidence |
|---|---|
| Bare `fm`, redirected `fm`, help, version, and JSON | Correct entry path, output, and exit code; redirected use never waits. |
| Search, Back, forms, and resize | Selection and input survive navigation; shortcuts do not corrupt text. |
| Success, failure, and interruption | Accurate result, restored terminal, no unobserved local child process. |
| Interactive child command | Child receives input and signals; the TUI returns after exit. |
| Missing repo, bad configuration, invalid manifest, and unavailable credentials | Actionable error; no false success or silent alternate target. |
| Nonzero report with valid JSON | Findings remain visible with the correct failure state. |
| Review and cancellation | No side effect before Run; Cancel starts no child process. |
| Narrow, wide, monochrome, and optional theme | Readable content, visible focus, recognizable logo, and reachable actions. |
| Logo on launch, resize, and return | Correct artwork size, no clipped or duplicate banner, and immediate menu input. |

Keep a repeatable terminal session recording, screenshots at both target sizes,
the exact reproduction commands, FM version, and test results as the release
evidence. Use a disposable workspace for write checks. Do not use live robot
motion or production data to verify navigation.

## Resolve During Product Review

- Confirm the proposed task labels with the people who use FM each day.
- Confirm the existing terminal palette as this surface's design contract.
- Select the first device or data workflow after the workspace release.

The plan can proceed to an interaction prototype with these defaults. Full
guided coverage of every repository command is a later extension.
