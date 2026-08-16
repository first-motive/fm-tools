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
uv pip install "fm-tools @ git+https://github.com/first-motive/fm-tools@v0.4.1"
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

`fm` is a thin CLI over every First Motive repo — one discoverable,
machine-readable surface for developers and AI agents landing cold. It ships as a
console entry point with the wheel.

Reporting verbs, all read-only and all taking `--json`:

| Verb        | Reports                                                        |
| ----------- | ------------------------------------------------------------- |
| `fm list`   | Every registered `fm-*` repo: name, git URL, entry points.    |
| `fm status` | Per-repo git state — branch, clean/dirty, ahead/behind. Repos not on disk are reported as `not cloned`, never faked. |
| `fm doctor` | Each repo's health checks — the declared ones (clone present, tools on `PATH`) plus derived ones (clone not behind origin, command manifest valid, installed `fm` matching its checkout). Exits non-zero when any check fails, so it drops into CI. |
| `fm commands` | Every verb this `fm` answers to — built-in, forwarding, and whatever the repos on this machine mount. The list an agent should read instead of scraping `--help`. |

`fm --version` prints the running build, and names the fm-tools checkout's
version when the two differ. That gap is worth printing: `fm` is installed from a
pinned tag while its source keeps moving, and an installed build that has fallen
behind simply does not have the verbs the checkout declares. `fm doctor` fails on
the same drift.

Every `--json` payload is wrapped in a versioned envelope, so a reader can tell
which contract it is reading:

```json
{"schema_version": 1, "verb": "status", "data": [ ... ]}
```

`data` is the verb's rows. The version bumps when a field changes meaning or
disappears — never when one is added, since a reader that ignores unknown keys
keeps working.

Verbs that act, each by handing the work to a repo's own script:

| Verb                        | Does                                                |
| --------------------------- | --------------------------------------------------- |
| `fm update`                 | Fast-forwards every clean clone, then runs that repo's update script. A dirty tree is skipped, never clobbered. |
| `fm install <repo> [args…]` | Runs that repo's `install.sh`, forwarding every argument. |
| `fm setup [--role R] [--dry-run]` | Clones what is missing, adopts existing clones in place, runs each installer, then prints `fm doctor`'s verdict. |

```bash
fm list                       # rich table
fm status --json              # machine-readable, parseable by an agent
fm doctor                     # exits non-zero if a check fails
fm setup --dry-run            # read the plan before anything is written
fm setup --role workstation   # stand up a GPU workstation
fm setup --role jetson        # stand up a Jetson capture rig
```

`--role` never changes which repos are set up. It decides what each repo's
installer is told: on a Jetson, `fm-setup` is given `--jetson` and `fm-ros2` is
given `--recorder --service`. Those flags are declared per repo in the registry,
because a flag one installer understands is an error to another.

A repo that names a platform is skipped elsewhere rather than cloned and failed
against — `fm-desktop` is a native macOS app, so a Linux setup run skips it, and
`fm-setup` provisions machines, so a macOS run skips that.

### Repo Commands

Repos mount their own verbs. A repo declares them in a top-level `fm.json`:

```json
{
  "version": 1,
  "commands": {
    "teleop": {"script": "scripts/run/teleop.sh", "help": "jog a robot arm"}
  }
}
```

Each entry becomes a flat verb, run from inside that repo's checkout with every
argument forwarded untouched:

```bash
fm teleop --robot openarm --backend mock    # runs fm_ros2's teleop.sh
```

The CLI parses none of those flags, so the script stays the single source of
truth for its own interface. Two repos claiming one verb is reported by
`fm doctor`, and the first in registry order keeps it; a manifest can never
shadow a built-in verb. `fm --help` lists whatever the repos on this machine
declare, and `fm commands --json` says the same thing to an agent.

#### Nouns, not just verbs

Because arguments are forwarded untouched, a repo can mount a **noun** and let
its script dispatch the verb:

```bash
fm machine init --name fm-rec-01   # runs fm-setup's scripts/run/machine.sh init
fm machine show --json
```

`fm.json` declares `machine` once; `init`, `show`, `doctor`, and `reset` live in
the script, where the work does. The dispatcher needs no knowledge of this and
must never gain any — noun-verb is a property of forwarding arguments verbatim,
and special-casing it in the CLI would put half of a repo's interface back in
this wheel. Prefer a noun whenever a repo has more than one or two related
workflows: one mounted name, no release of `fm-tools` to add a verb behind it.

### Workspace Root

Every verb resolves repos under one workspace root, chosen in this order:

1. `FM_HOME`, if set
2. `~/.config/fm/config.json`, holding `{"root": "/path/to/workspace"}`
3. detection — the nearby directory holding the most registered clones
4. `~`

Detection adopts an existing layout exactly where it is; `fm` never moves a
checkout.

### Install the CLI

`uv pip install` above imports the wheel; it does not put `fm` on your `PATH`. To
install the CLI system-wide for a developer, run the installer from a clone:

```bash
git clone https://github.com/first-motive/fm-tools
cd fm-tools && ./install.sh          # uv tool install; fm + fm-pick onto PATH
```

`./install.sh` installs a pinned release tag as an isolated `uv` tool. Other
subcommands:

```bash
./install.sh status                  # is fm installed and on PATH?
./install.sh install --dry-run       # print the resolved install spec, install nothing
./install.sh uninstall               # remove the fm CLI
```

If `fm` is not found after install, run `uv tool update-shell` once, then restart
your shell. Override the release with `FM_TOOLS_REF` (git tag) or `FM_TOOLS_REPO`
(owner/repo); the default tag tracks this wheel's version.

### Boundary: delegate, never duplicate

The CLI owns discovery, routing, and reporting — nothing else. Each repo keeps
its own bootstrap front door (`install.sh` / `run.sh`) and its own workflow
scripts; `fm` finds them, runs them, and reports what happened. It shells out to
`git` for state, and it never reimplements what a repo already does. The repo
registry is an in-package Python module (not TOML) so it stays zero-dependency
and packages with the wheel; the verbs a repo exposes live in that repo's
`fm.json`, so adding one needs no release of this wheel.

Still deferred: GitHub org auto-discovery, a `--stable` release channel, and an
interactive `pick` menu over the verbs.

## Development

```bash
uv run pytest
```

See `CONTRIBUTING.md` for the branch, commit, and PR workflow.

## License

Apache-2.0 — see `LICENSE`.
