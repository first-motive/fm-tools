"""fm root — the verb that answers "which workspace is fm talking about"."""

import json

from fm_tools.cli import main


def test_root_json_names_the_root_and_its_source(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["root", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["data"]
    assert row["root"] == str(tmp_path)
    assert row["source"] == "env"
    assert row["exists"] is True


def test_root_reports_a_card_as_the_source(tmp_path, monkeypatch, capsys):
    card = tmp_path / "machine.json"
    card.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "fm-rec-01",
                "role": "jetson",
                "fleet": "prod",
                "transport": "zenoh",
                "workspace": str(tmp_path / "fm"),
            }
        )
    )
    monkeypatch.setenv("FM_MACHINE_FILE", str(card))
    assert main(["root", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["data"]
    assert row["root"] == str(tmp_path / "fm")
    assert row["source"] == "card"
    assert "fm-rec-01" in row["detail"]


def test_root_table_renders(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    assert main(["root"]) == 0
    assert "fm root" in capsys.readouterr().out


def test_status_is_identical_from_any_directory(tmp_path, monkeypatch, capsys):
    """The acceptance criterion for deterministic roots, stated as a test."""
    monkeypatch.setenv("FM_HOME", str(tmp_path))
    elsewhere = tmp_path / "elsewhere" / "deep"
    elsewhere.mkdir(parents=True)

    monkeypatch.chdir(elsewhere)
    main(["status", "--json", "--no-fetch"])
    from_deep = capsys.readouterr().out

    monkeypatch.chdir(tmp_path)
    main(["status", "--json", "--no-fetch"])
    assert capsys.readouterr().out == from_deep
