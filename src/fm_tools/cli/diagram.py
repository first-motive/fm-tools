"""fm diagram — every diagram in the workspace, as one list a person or an app can read.

Each repo already renders its own diagrams: ``docs/diagrams/render.sh`` is a
render-plane artifact, identical in seventeen repos, and the diagram gate makes
CI fail on a committed SVG that no longer matches its source. What was missing is
the view *across* repos. A picture of the system is spread over fm-ros2, fm-data,
fm-comms and the rest, and until this verb the only way to see the set was to
remember which repos had one and open each in turn.

``fm diagram list --json`` is that set, machine-readable: one row per ``.d2``
source with its rendered sidecar beside it. Desktop's diagram surface reads this
manifest rather than carrying its own list, so a diagram added in any repo shows
up in the app with no Swift change — the same relationship the launch registry
already has with the robots.

The other three verbs are the authoring loop. ``render`` and ``check`` delegate
to each repo's own ``render.sh``, never re-implementing it: the gate CI runs and
the command a developer runs have to be the same command, or a green check means
nothing. ``watch`` is the one thing no repo script owns — an fswatch over every
``.d2`` in the workspace that re-renders the repo a file belongs to, so the SVG
under review is never the stale one.

    fm diagram list [--json]     every diagram, and whether it is rendered
    fm diagram render [--repo R] re-render, through each repo's render.sh
    fm diagram check [--repo R]  fail on a diagram whose SVG drifted
    fm diagram watch [--repo R]  re-render on every save (needs fswatch)
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import exits
from .payload import emit

# The renderer every diagram-carrying repo has, rendered there by the plane. Its
# presence is what makes a checkout a diagram repo — no list of repo names here,
# so a new repo joins the workspace view by adopting the artifact, not by being
# named in fm-tools.
RENDERER = Path("docs/diagrams/render.sh")

# styles.d2 is an import-only palette, not a diagram. render.sh skips it by the
# same rule, and the two must agree or this verb lists a file the renderer will
# never draw.
PALETTE = "styles.d2"

# Directories never worth walking for sources: git's object store, and the build
# trees a colcon workspace leaves beside the packages.
SKIP_DIRS = frozenset({".git", "build", "install", "log"})


@dataclass(frozen=True)
class Diagram:
    """One ``.d2`` source in the workspace, and the render committed beside it."""

    repo: str
    name: str
    source: Path
    svg: Path
    rendered: bool

    @property
    def title(self) -> str:
        """The file stem as a human reads it: ``capture_lifecycle`` → Capture Lifecycle."""
        return self.name.replace("_", " ").replace("-", " ").title()


def _sources(repo: Path) -> list[Path]:
    """Every diagram source in one checkout, in the order render.sh walks them."""
    found = []
    for path in sorted(repo.rglob("*.d2")):
        if path.name == PALETTE:
            continue
        if SKIP_DIRS.intersection(path.relative_to(repo).parts):
            continue
        found.append(path)
    return found


def repos(root: Path, only: str = "") -> list[Path]:
    """The diagram-carrying checkouts under ``root``, optionally just one.

    A checkout qualifies by having the rendered ``render.sh``, so the answer
    tracks whichever repos the render plane reaches. ``only`` matches the
    directory name, which is what a developer has in front of them — fm-ros2
    clones as ``fm_ros2``, so both spellings resolve to the same checkout.
    """
    if not root.is_dir():
        return []
    wanted = {only, only.replace("-", "_")} if only else set()
    return sorted(
        path
        for path in root.iterdir()
        if (path / RENDERER).is_file() and (not wanted or path.name in wanted)
    )


def diagrams(root: Path, only: str = "") -> list[Diagram]:
    """Every diagram in the workspace, one row per source.

    A multi-board diagram lands as a *directory* of boards rather than one
    sidecar, so both shapes count as rendered — the same case render.sh handles
    when it compares.
    """
    found = []
    for repo in repos(root, only):
        for source in _sources(repo):
            svg = source.with_suffix(".svg")
            boards = source.with_suffix("")
            found.append(
                Diagram(
                    repo=repo.name,
                    name=source.stem,
                    source=source,
                    svg=svg,
                    rendered=svg.is_file() or boards.is_dir(),
                )
            )
    return found


def _payload(found: list[Diagram]) -> list[dict]:
    """The ``list`` verb's rows. Paths are absolute: the reader is another process."""
    return [
        {
            "repo": diagram.repo,
            "name": diagram.name,
            "title": diagram.title,
            "source": str(diagram.source),
            "svg": str(diagram.svg),
            "rendered": diagram.rendered,
        }
        for diagram in found
    ]


def _render_table(found: list[Diagram]) -> None:
    table = Table(title="fm diagram")
    table.add_column("repo", style="bold")
    table.add_column("diagram")
    table.add_column("source")
    table.add_column("render")
    for diagram in found:
        table.add_row(
            diagram.repo,
            diagram.title,
            str(diagram.source.parent.name + "/" + diagram.source.name),
            "committed" if diagram.rendered else "missing",
        )
    Console().print(table)


def _delegate(found: list[Path], check: bool, dry_run: bool) -> int:
    """Run each repo's own render.sh, and report the class of what came back."""
    failed = []
    for repo in found:
        command = [str(repo / RENDERER), *(["--check"] if check else [])]
        if dry_run:
            print(" ".join(command))
            continue
        print(f"== {repo.name} ==")
        try:
            code = subprocess.run(command, cwd=repo, check=False).returncode
        except OSError as exc:
            exits.fail(f"{repo.name}: {exc}")
            return exits.PRECONDITION
        if code != 0:
            failed.append(repo.name)
    if not failed:
        return exits.OK
    exits.fail(f"{'drifted' if check else 'failed to render'}: {', '.join(failed)}")
    # A drifted diagram is a true answer about an unhealthy repo; a renderer that
    # could not draw is a delegate that failed. The exit table tells them apart.
    return exits.UNHEALTHY if check else exits.DELEGATE


def _watch(found: list[Path]) -> int:
    """Re-render a repo whenever one of its ``.d2`` files is written.

    fswatch rather than a poll loop: a diagram is edited in bursts, and a poll
    slow enough to be cheap is slow enough to show the previous drawing. The
    whole workspace is watched in one process, and each event re-renders only the
    repo the changed file belongs to — re-rendering all seventeen on every save
    would take long enough that the developer stops saving.
    """
    if shutil.which("fswatch") is None:
        exits.fail("fswatch is not on PATH — install it (brew install fswatch) to watch")
        return exits.PRECONDITION

    watched = {repo: _sources(repo) for repo in found}
    paths = [str(source) for sources in watched.values() for source in sources]
    if not paths:
        exits.fail("no diagram sources to watch")
        return exits.PRECONDITION

    print(f"fm: watching {len(paths)} diagram(s) in {len(watched)} repo(s) — ctrl-c to stop")
    try:
        with subprocess.Popen(
            ["fswatch", "--one-per-batch=0", *paths],
            stdout=subprocess.PIPE,
            text=True,
        ) as watcher:
            for line in watcher.stdout or ():
                changed = Path(line.strip())
                repo = next((repo for repo in watched if changed.is_relative_to(repo)), None)
                if repo is None:
                    continue
                print(f">> {changed.name} changed — rendering {repo.name}")
                subprocess.run([str(repo / RENDERER)], cwd=repo, check=False)
    except KeyboardInterrupt:
        return exits.INTERRUPTED
    return exits.OK


USAGE = """usage: fm diagram <verb> [args...]

  list [--json]              every .d2 in the workspace, and its committed render
  render [--repo R]          re-render, through each repo's own render.sh
  check [--repo R]           fail on a diagram whose committed SVG drifted
  watch [--repo R]           re-render on every save (needs fswatch)

  --dry-run                  print what render/check would run, and run nothing

Repos are discovered by the rendered docs/diagrams/render.sh, so a repo joins
this view by adopting the render plane's artifact."""


class UsageError(ValueError):
    """An argument shape the verb refuses rather than reinterprets."""


def _flag_value(argv: list[str], flag: str) -> tuple[str, list[str]]:
    """Pull ``--flag value`` or ``--flag=value`` out of an argument list.

    A bare trailing ``--flag`` is a usage error, not an absent flag: reading it
    as "no value" would silently widen ``--repo`` to every repo.
    """
    rest = list(argv)
    for index, arg in enumerate(rest):
        if arg == flag:
            if index + 1 >= len(rest) or rest[index + 1].startswith("-"):
                raise UsageError(f"{flag} needs a value")
            return rest[index + 1], rest[:index] + rest[index + 2 :]
        if arg.startswith(f"{flag}="):
            value = arg.split("=", 1)[1]
            if not value:
                raise UsageError(f"{flag} needs a value")
            return value, rest[:index] + rest[index + 1 :]
    return "", rest


def run_diagram(argv: list[str], root: Path) -> int:
    """``fm diagram <verb>`` handler, parsed by hand like the other nouns."""
    verb = argv[0] if argv else ""
    rest = argv[1:]

    if not verb or verb in ("-h", "--help"):
        print(USAGE)
        return exits.OK if verb else exits.USAGE
    if verb not in ("list", "render", "check", "watch"):
        exits.fail(f"unknown diagram verb {verb!r} (use list|render|check|watch)")
        return exits.USAGE

    try:
        only, rest = _flag_value(rest, "--repo")
    except UsageError as error:
        exits.fail(str(error))
        return exits.USAGE
    dry_run = "--dry-run" in rest
    rest = [arg for arg in rest if arg != "--dry-run"]

    found = repos(root, only)
    if not found:
        where = f"{only!r} under {root}" if only else str(root)
        exits.fail(f"no repo with {RENDERER} in {where}")
        return exits.PRECONDITION

    if verb == "list":
        rows = diagrams(root, only)
        if "--json" in rest:
            emit("diagram", _payload(rows))
        else:
            _render_table(rows)
        return exits.OK

    if verb == "watch":
        return _watch(found)

    return _delegate(found, check=verb == "check", dry_run=dry_run)
