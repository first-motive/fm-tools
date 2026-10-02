# Use The FM Terminal Interface

Run `fm` with no arguments in a terminal. Use task groups or type to search.
Reports open directly. Other actions show their arguments, workspace, command,
and effects before they run. The approved logo appears without text below it.

Use arrows and Enter to select, Tab to move between controls, Escape to go
back, and Ctrl+Q to quit. `/` focuses search and `?` shows help outside a field.
Set `FM_TUI_ASCII=1` for an ASCII logo. Set `NO_COLOR=1` for monochrome output.
The terminal font controls glyph appearance. No image library is needed.

`fm <command>` keeps its existing behavior. Bare `fm` with redirected input or
output prints help and returns usage code 2. `TERM=dumb` uses the same fallback.

## Run Commands Through One Interface

`app.py` reads the CLI catalogue. Known report argument lists run as asynchronous
children and return versioned JSON. Health checks and remote Git fetches require
review. New repository commands appear automatically, with literal arguments
parsed by `shlex`; shell expansion and pipes are not supported.

A command's `fm.json` entry can name its task group with `"group"`: one of
`workspace`, `device`, `data`, `robot`, `develop`, or `maintain`. Without it,
`group()` in `app.py` guesses from the verb, and an unknown verb goes only to
**Browse all commands**. An invalid group is a doctor problem; the verb stays
mounted.

Report lists show as tables that start at the first row. Health checks show a
count for each level and list failures, then warnings, before passes.

For terminal commands, `runner.py` waits until Textual has restored the terminal,
then starts the same FM interpreter and installation. The child owns terminal
input and signals. Press Enter after it exits to restore the prior menu and see
the result. Run again returns through review. The workspace is fixed to the
reviewed root with `FM_HOME`; repository scripts keep their own working directory.

Only one command runs at a time. An interrupt waits for the report to stop;
quitting cannot abandon it. Remote services may outlive their launch command.
An interrupt is not a robot emergency stop. Existing credential and human
approval checks remain in the CLI and the owning repository.

The app uses the shared palette directly so optional `nish-tui` cannot change
its colors. Existing `fm-pick` callers retain the theme layer they already use.
The source, hash, crop, and sample sizes for the logo are in `logo.py`.

## Verify The Interface

From the repository root:

```sh
uv run --extra dev pytest tests/test_pick.py tests/test_cli_root.py
FM_TUI_EVIDENCE_DIR=/tmp/fm-tui-evidence uv run --extra dev pytest \
  tests/test_pick.py::test_fm_terminal_handoff_and_interrupt -q
```

The terminal E2E check opens the actual FM entry point in a PTY. It uses a
throwaway manifest command to verify input, failure status, retry, interruption,
and return. It writes `terminal-session.ansi` to the requested evidence directory.
The other interface checks drive Textual with a real report subprocess, review
cancellation, argument quoting, secret rejection, and resize.

The product plan is in [docs/tui-plan.md](../../../docs/tui-plan.md).
Further guided device and data forms remain the plan's usage-led follow-up.
