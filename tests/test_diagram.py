"""fm diagram — the workspace-wide view over per-repo renderers."""

import json

import pytest

from fm_tools.cli import diagram, exits, main


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Two diagram repos and one repo that has no renderer, under one root."""

    def repo(name, *, renderer=True):
        root = tmp_path / name
        (root / "docs" / "diagrams").mkdir(parents=True)
        if renderer:
            script = root / diagram.RENDERER
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
        return root

    ros2 = repo("fm_ros2")
    (ros2 / "docs" / "diagrams" / "loop.d2").write_text("a -> b\n")
    (ros2 / "docs" / "diagrams" / "loop.svg").write_text("<svg/>\n")
    (ros2 / "docs" / "diagrams" / "styles.d2").write_text("# palette\n")
    # A build tree is not a source of diagrams, even when a vendored package
    # left one there.
    (ros2 / "build" / "pkg").mkdir(parents=True)
    (ros2 / "build" / "pkg" / "copied.d2").write_text("a -> b\n")

    data = repo("fm-data")
    (data / "docs" / "diagrams" / "capture_lifecycle.d2").write_text("a -> b\n")

    repo("fm-notes", renderer=False)
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    return tmp_path


def test_lists_every_repo_that_adopted_the_renderer(workspace):
    """Membership follows the rendered render.sh, not a list of repo names."""
    assert [path.name for path in diagram.repos(workspace)] == ["fm-data", "fm_ros2"]


def test_palette_and_build_trees_are_not_diagrams(workspace):
    """styles.d2 is an import, and a build tree is a copy — render.sh skips both."""
    names = {row.name for row in diagram.diagrams(workspace)}
    assert names == {"capture_lifecycle", "loop"}


def test_reports_which_diagrams_have_a_committed_render(workspace):
    """The manifest's whole job: which picture exists, and which is missing."""
    rendered = {row.name: row.rendered for row in diagram.diagrams(workspace)}
    assert rendered == {"loop": True, "capture_lifecycle": False}


def test_multi_board_directory_counts_as_rendered(workspace):
    """d2 writes a directory of boards for a layered diagram, not one sidecar."""
    (workspace / "fm-data" / "docs" / "diagrams" / "capture_lifecycle").mkdir()
    rendered = {row.name: row.rendered for row in diagram.diagrams(workspace)}
    assert rendered["capture_lifecycle"] is True


def test_repo_filter_accepts_either_spelling(workspace):
    """fm-ros2 clones as fm_ros2; a developer should not have to remember which."""
    assert [path.name for path in diagram.repos(workspace, "fm-ros2")] == ["fm_ros2"]
    assert [path.name for path in diagram.repos(workspace, "fm_ros2")] == ["fm_ros2"]


def test_list_json_is_the_versioned_envelope(workspace, capsys):
    """Desktop reads this payload; it gets the same envelope every verb emits."""
    assert main(["diagram", "list", "--json"]) == exits.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["verb"] == "diagram"
    assert {row["repo"] for row in payload["data"]} == {"fm-data", "fm_ros2"}
    assert payload["data"][0]["title"] == "Capture Lifecycle"


def test_dry_run_prints_the_delegate_and_runs_nothing(workspace, capsys):
    """--dry-run names the repo's own script, which is the point: fm never renders."""
    assert main(["diagram", "render", "--dry-run"]) == exits.OK
    printed = capsys.readouterr().out
    assert str(workspace / "fm_ros2" / diagram.RENDERER) in printed


def test_check_reports_drift_as_unhealthy(workspace, capsys):
    """A drifted SVG is a true answer about a bad state, not a broken delegate."""
    script = workspace / "fm-data" / diagram.RENDERER
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)
    assert main(["diagram", "check"]) == exits.UNHEALTHY
    assert "drifted: fm-data" in capsys.readouterr().err


def test_render_failure_is_a_delegate_failure(workspace, capsys):
    """The same non-zero from `render` means the renderer itself failed."""
    script = workspace / "fm-data" / diagram.RENDERER
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)
    assert main(["diagram", "render"]) == exits.DELEGATE


def test_unknown_verb_is_a_usage_error(workspace, capsys):
    assert main(["diagram", "sketch"]) == exits.USAGE
    assert "unknown diagram verb" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--repo"], ["--repo="], ["--repo", "--dry-run"]])
def test_a_bare_repo_flag_is_refused_not_widened(workspace, capsys, argv):
    # Reading a valueless --repo as "no filter" would render every repo the
    # caller meant to narrow to one.
    assert main(["diagram", "render", *argv]) == exits.USAGE
    assert "--repo needs a value" in capsys.readouterr().err


def test_a_workspace_with_no_diagram_repo_is_a_precondition_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["diagram", "list"]) == exits.PRECONDITION
    assert "no repo with" in capsys.readouterr().err


def test_watch_without_fswatch_says_so(workspace, monkeypatch, capsys):
    """The one verb with an external dependency names it instead of failing oddly."""
    monkeypatch.setattr(diagram.shutil, "which", lambda _: None)
    assert main(["diagram", "watch"]) == exits.PRECONDITION
    assert "fswatch" in capsys.readouterr().err


def test_diagram_is_reported_as_a_verb(workspace, capsys):
    """`fm commands` is the surface an agent reads; a verb missing there is unreachable."""
    assert main(["commands", "--json"]) == exits.OK
    payload = json.loads(capsys.readouterr().out)
    row = next(entry for entry in payload["data"] if entry["verb"] == "diagram")
    assert row["kind"] == "forwarding"
