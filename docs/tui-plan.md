# FM Terminal Workspace

Status: implemented in 0.27.0. Bare `fm` opens a persistent workspace with Repos,
Health, Updates, Workflows, and Activity. The CLI remains the execution interface
under these screens. The TUI collects named inputs, confirms effects, shows live
progress, and retains results inside the app.

The prior command-launcher design is replaced. Users do not type argument strings
or leave the app to answer a workflow prompt. Nested terminal menus use an
embedded terminal pane. The existing broker, approvals, and robot guards remain
in the command owners.

The [interface guide](../src/fm_tools/tui/README.md) describes controls, execution
boundaries, manifest extensions, parser snapshot updates, and verification.

## Acceptance Evidence

The existing E2E suite checks repo and health reports, selectable form values,
required alternatives, update results from a disposable Git remote, failure and
retry, result history, and real-terminal input and interruption. A nested FM menu
runs inside the real-terminal flow. Screens are checked at 80×24 and 100×32.

A parser-only audit checks exported forms against their owner parsers without
running handlers. A catalogue audit checks that each mounted workflow verb has
controls and that action identities are unique. These checks do not claim that
remote robot, cloud, or hardware workflows were executed during development.
