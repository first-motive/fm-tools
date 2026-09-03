# CLAUDE.md

Guidance for Claude Code and Codex working in this repo.

## Purpose

`fm-tools` is First Motive's shared terminal tooling as a pip-installable wheel:
the brand palette, step banners, themed widgets, and a generic `pick` menu. It is
pure Python (textual + rich) with no `rclpy`, so any repo can depend on it from a
git tag without a ROS environment.

## Conventions

- Commit and branch rules live in `CONTRIBUTING.md`. Follow them.
- Commits are subject-line-only: `prefix: phrase`. No body.
- Python tooling goes through `uv` — never bare `pip`, `python`, or `poetry`.
- No `rclpy` or ROS imports — the wheel must stay ROS-free.

## Testing

```bash
uv run pytest
```

## The `fm` CLI

`fm` is a cross-repo dispatcher over the First Motive repos. Reporting verbs,
each with `--json` for agents and CI:

- `fm list` — every registered `fm-*` repo (name, URL, entry points)
- `fm status` — per-repo git state; repos not on disk report `not cloned`
- `fm doctor` — health checks, including manifest validity; exits non-zero on
  failure, so it fits CI

Verbs that act, each by delegating to a repo's own script:

- `fm update` — fast-forward every clean clone, then run its update script
- `fm install <repo> [args…]` — run that repo's `install.sh`, args forwarded
- `fm setup [--role workstation|jetson|mac|trainer] [--dry-run]` — clone what is missing,
  adopt what exists, install, then print doctor's verdict. The role decides each
  installer's arguments (declared per repo in `registry.py`, never passed
  through from the command line); a repo that names a platform is skipped on
  every other one.
- anything a repo declares in its `fm.json` (see the contract below)

Repos are resolved under one workspace root: `FM_HOME`, else
`~/.config/fm/config.json`, else detection, else `~`.

`fm` and `fm-pick` are console entry points in the wheel. `./install.sh` puts
them on `PATH` via `uv tool install` (fm-tools is a tool-installer, not just a
library). The CLI never reimplements a repo's bootstrap — it delegates.

## fm CLI Contract

`fm` mounts a repo's workflows as top-level verbs by reading `fm.json` at that
repo's root. A new user-facing workflow script must be declared there, or it
stays unreachable from `fm`. The rule binds every First Motive repo, including
this one.

- Declare it: add `"<verb>": {"script": "<path/to/script.sh>", "help": "<one line>"}`
  under `commands` in the repo's `fm.json`.
- Verify it: `fm doctor` fails on a declared script that is missing or not
  executable, and warns on a run script that no manifest declares.

Arguments are forwarded to the script verbatim — the CLI parses none of them, so
the script stays the single source of truth for its own flags. See the `fm-cli`
skill for the manifest schema and the full verb surface.

## Layout

- `src/fm_tools/tui/` — palette, banner, widgets, theme, and the `pick` menu
- `src/fm_tools/cli/` — the `fm` dispatcher, repo registry, and read verbs
- `install.sh`, `scripts/` — tool-installer front door and its `uv tool` verb
- `tests/` — pytest suite (the picker drives Textual's async pilot)
