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

## Layout

- `src/fm_tools/tui/` — palette, banner, widgets, theme, and the `pick` menu
- `tests/` — pytest suite (the picker drives Textual's async pilot)
