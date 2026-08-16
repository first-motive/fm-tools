"""fm install tests — repo resolution, delegation, and arg forwarding."""

from fm_tools.cli import exits, main
from fm_tools.cli.install import USAGE_ERROR, find_repo, run_install


def _clone(base, local_dir, installer_body="#!/bin/sh\nexit 0\n", executable=True):
    """Materialise a checkout with an install.sh front door."""
    checkout = base / local_dir
    (checkout / ".git").mkdir(parents=True)
    installer = checkout / "install.sh"
    installer.write_text(installer_body)
    if executable:
        installer.chmod(0o755)
    return checkout


def test_repo_resolves_by_name_or_directory():
    assert find_repo("fm-ros2") is find_repo("fm_ros2")
    assert find_repo("fm-ros2").name == "fm-ros2"


def test_unknown_repo_is_a_usage_error(tmp_path, capsys):
    assert run_install(["fm-nope"], tmp_path) == USAGE_ERROR
    assert "unknown repo" in capsys.readouterr().err


def test_no_repo_named_prints_usage(tmp_path, capsys):
    assert run_install([], tmp_path) == USAGE_ERROR
    assert "usage: fm install" in capsys.readouterr().out


def test_help_exits_clean(tmp_path, capsys):
    assert run_install(["--help"], tmp_path) == 0
    assert "usage: fm install" in capsys.readouterr().out


def test_uncloned_repo_points_at_setup(tmp_path, capsys):
    assert run_install(["fm-ros2"], tmp_path) == exits.PRECONDITION
    assert "fm setup" in capsys.readouterr().err


def test_missing_installer_fails(tmp_path, capsys):
    (tmp_path / "fm_ros2" / ".git").mkdir(parents=True)
    assert run_install(["fm-ros2"], tmp_path) == exits.PRECONDITION
    assert "no install.sh" in capsys.readouterr().err


def test_non_executable_installer_fails(tmp_path, capsys):
    _clone(tmp_path, "fm_ros2", executable=False)
    assert run_install(["fm-ros2"], tmp_path) == exits.PRECONDITION
    assert "not executable" in capsys.readouterr().err


def test_args_reach_the_installer_verbatim(tmp_path):
    checkout = _clone(tmp_path, "fm_ros2", '#!/bin/sh\nprintf "%s\\n" "$@" > args.txt\n')
    assert run_install(["fm-ros2", "--native", "--role", "recorder"], tmp_path) == 0
    assert (checkout / "args.txt").read_text().split() == ["--native", "--role", "recorder"]


def test_installer_runs_inside_its_checkout(tmp_path):
    checkout = _clone(tmp_path, "fm_ros2", "#!/bin/sh\npwd > cwd.txt\n")
    run_install(["fm-ros2"], tmp_path)
    assert (checkout / "cwd.txt").read_text().strip() == str(checkout)


def test_the_script_about_to_run_is_announced(tmp_path, capsys):
    checkout = _clone(tmp_path, "fm_ros2")
    run_install(["fm-ros2"], tmp_path)
    assert str(checkout / "install.sh") in capsys.readouterr().err


def test_installer_exit_code_passes_through(tmp_path):
    _clone(tmp_path, "fm_ros2", "#!/bin/sh\nexit 5\n")
    assert run_install(["fm-ros2"], tmp_path) == 5


def test_install_verb_dispatches_via_main(tmp_path, monkeypatch):
    _clone(tmp_path, "fm_ros2", "#!/bin/sh\nexit 4\n")
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["install", "fm-ros2", "--native"]) == 4


def test_manifest_cannot_shadow_install(tmp_path, monkeypatch, capsys):
    import json

    checkout = _clone(tmp_path, "fm_ros2", "#!/bin/sh\nexit 4\n")
    (checkout / "fm.json").write_text(
        json.dumps({"version": 1, "commands": {"install": {"script": "install.sh"}}})
    )
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    # The built-in wins: the installer runs, not the manifest's claim on the name.
    assert main(["install", "fm-ros2"]) == 4
