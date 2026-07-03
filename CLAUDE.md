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

`fm` is a read-only, cross-repo dispatcher over the First Motive repos. Three
verbs, each with `--json` for agents and CI:

- `fm list` — every registered `fm-*` repo (name, URL, entry points)
- `fm status` — per-repo git state; repos not on disk report `not cloned`
- `fm doctor` — declared health checks; exits non-zero on failure, so it fits CI

`fm` and `fm-pick` are console entry points in the wheel. `./install.sh` puts
them on `PATH` via `uv tool install` (fm-tools is a tool-installer, not just a
library). The CLI reports state only — it never clones or runs a repo's
bootstrap.

## Layout

- `src/fm_tools/tui/` — palette, banner, widgets, theme, and the `pick` menu
- `src/fm_tools/cli/` — the `fm` dispatcher, repo registry, and read verbs
- `install.sh`, `scripts/` — tool-installer front door and its `uv tool` verb
- `tests/` — pytest suite (the picker drives Textual's async pilot)
