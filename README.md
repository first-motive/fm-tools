# fm-tools

First Motive's shared terminal tooling, as a pip-installable wheel.

## What

`fm-tools` carries the reusable, ROS-free half of First Motive's terminal UI:
the brand palette, the colour-coded step banners, the themed widget set, and a
generic `pick` menu. It was carved out of `fm-app`'s `fm_tui` package so any
First Motive repo can share one source of brand and one picker without pulling in
`rclpy` or the rest of the app.

The wheel depends only on `textual` and `rich` — no ROS, no `rclpy`. It imports
cleanly anywhere Python runs.

## Install

Distribution is git tag-pinned (PyPI-ready, not yet published):

```bash
uv pip install "fm-tools @ git+https://github.com/first-motive/fm-tools@v0.2.0"
```

## Usage

In Python:

```python
from fm_tools.tui import pick, emit

emit(1, "Detect OS")                       # branded step banner
backend = pick("Pick a backend", ["mujoco", "gazebo", "isaac"])
```

From a shell script (the `fm-pick` console entry prints the choice to stdout):

```bash
backend=$(fm-pick "Pick a backend" mujoco gazebo isaac)
```

## The `fm` CLI

`fm` is a thin, read-only CLI over every First Motive repo — one discoverable,
machine-readable surface for developers and AI agents landing cold. It ships as a
console entry point with the wheel.

Three verbs, all read-only:

| Verb        | Reports                                                        |
| ----------- | ------------------------------------------------------------- |
| `fm list`   | Every registered `fm-*` repo: name, git URL, entry points.    |
| `fm status` | Per-repo git state — branch, clean/dirty, ahead/behind. Repos not on disk are reported as `not cloned`, never faked. |
| `fm doctor` | Each repo's declared health checks (clone present, tools on `PATH`). Exits non-zero when any check fails, so it drops into CI. |

Every verb takes `--json` for agents and CI; the default is a rich table for
humans:

```bash
fm list                 # rich table
fm status --json        # machine-readable, parseable by an agent
fm doctor               # exits non-zero if a check fails
```

`fm status` and `fm doctor` resolve each repo under the workspace root — the
parent of the directory `fm` runs in — so running it from inside one repo finds
its siblings.

### Boundary: delegate, never duplicate

The CLI owns cross-repo verbs only. Each repo keeps its own bootstrap front door
(`install.sh` / `run.sh`); `fm` never reimplements that logic. v1 is read-only:
it shells out to `git` for status and resolves declared checks for doctor,
nothing more. The repo registry is an in-package Python module (not TOML) so it
stays zero-dependency and packages with the wheel.

Deferred to v2: delegating verbs (`install`, `run`, `update`), GitHub org
auto-discovery, an external config-file override, and an interactive `pick` menu.

## Development

```bash
uv run pytest
```

See `CONTRIBUTING.md` for the branch, commit, and PR workflow.

## License

Apache-2.0 — see `LICENSE`.
