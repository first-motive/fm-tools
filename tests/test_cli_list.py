"""fm list tests — both output modes drive the dispatcher via its exit contract."""

import json

import pytest

from fm_tools.cli import main
from fm_tools.cli.registry import REPOS

EXPECTED = {repo.name for repo in REPOS}


def test_list_table_prints_every_repo(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    for name in EXPECTED:
        assert name in out


def test_list_json_is_valid_and_complete(capsys):
    assert main(["list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {entry["name"] for entry in payload} == EXPECTED
    # Every entry carries the fields an agent needs to act on a repo.
    for entry in payload:
        assert entry["url"].startswith("https://github.com/first-motive/")
        assert entry["local_dir"]
        assert isinstance(entry["entry_points"], list)
        assert entry["entry_points"]


def test_no_verb_is_a_usage_error():
    # argparse exits 2 on a missing required subcommand.
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_unknown_verb_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        main(["frobnicate"])
    assert exc.value.code == 2
