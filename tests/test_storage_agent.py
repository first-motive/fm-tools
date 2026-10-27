"""The agent's fabric side: where it finds the router, how it bounds work, what it never answers."""

from __future__ import annotations

import json
import threading

import pytest

from fm_tools import storage_agent
from fm_tools.storage_agent import Fabric, Lane, router_endpoint


def test_the_environment_names_the_router_first(tmp_path, monkeypatch):
    env = tmp_path / "fm-comms.env"
    env.write_text("# fleet\nFM_ROUTER_ENDPOINT='tcp/10.0.0.2:7447'\n")
    monkeypatch.setenv("FM_COMMS_ENV_FILE", str(env))
    monkeypatch.delenv("FM_ROUTER_ENDPOINT", raising=False)
    assert router_endpoint() == "tcp/10.0.0.2:7447"
    monkeypatch.setenv("FM_ROUTER_ENDPOINT", "tcp/10.0.0.3:7447")
    assert router_endpoint() == "tcp/10.0.0.3:7447"


def test_no_router_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_COMMS_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("FM_ROUTER_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError, match="FM_ROUTER_ENDPOINT"):
        router_endpoint()


def test_a_full_lane_refuses_and_frees_its_slot_after_a_failure():
    lane, release = Lane("test", 1, 1), threading.Event()
    assert lane.submit(release.wait)
    assert not lane.submit(lambda: None)
    release.set()
    lane.pool.shutdown(wait=True)
    failing = Lane("test", 1, 1)
    assert failing.submit(lambda: 1 / 0)
    failing.pool.shutdown(wait=True)
    assert failing.slots.acquire(blocking=False), "a failed job must release its slot"


class Query:
    def __init__(self, key: str, payload: bytes | None):
        self.key_expr = key
        self.payload = None if payload is None else Payload(payload)


class Payload(bytes):
    def to_bytes(self) -> bytes:
        return bytes(self)


class Session:
    def __init__(self):
        self.handlers = {}

    def declare_queryable(self, key, handler):
        self.handlers[key] = handler
        return type("Queryable", (), {"undeclare": lambda self: None})()


class Agent:
    host, name = "fm_ws_01", "fm-ws-01"

    def __init__(self):
        self.answered = []

    def status(self):
        return {"ok": True, "host": self.host}

    def authorise(self, request):
        self.answered.append(request)
        return "nish"

    def run(self, request, actor):
        return {"ok": True}


@pytest.fixture
def fabric():
    session, agent, replies = Session(), Agent(), []
    fabric = Fabric(session, agent, reply=lambda query, key, body: replies.append((key, body)))
    yield session, agent, replies
    fabric.close()


def test_status_answers_under_this_hosts_own_key(fabric):
    session, _, replies = fabric
    session.handlers["fm/storage/fm_ws_01/status"](Query("fm/storage/*/status", None))
    assert replies == [("fm/storage/fm_ws_01/status", {"ok": True, "host": "fm_ws_01"})]


@pytest.mark.parametrize("key, payload, code", [
    ("fm/storage/*/request", b"{}", None),
    ("fm/storage/fm_ws_01/request", b"x" * (storage_agent.MAX_BODY_BYTES + 1), "invalid_request"),
    ("fm/storage/fm_ws_01/request", b"{}", "invalid_request"),
    ("fm/storage/fm_ws_01/request", json.dumps({"payload": "e30="}).encode(), "signature_required"),
])
def test_nothing_unsigned_or_unaddressed_reaches_the_agent(fabric, key, payload, code):
    session, agent, replies = fabric
    session.handlers["fm/storage/fm_ws_01/request"](Query(key, payload))
    assert agent.answered == []
    assert [body["error_code"] for _, body in replies] == ([] if code is None else [code])


def test_the_cli_prints_compact_json_only_when_asked(tmp_path, monkeypatch, capsys):
    from fm_tools.archive_workflow import main

    monkeypatch.setenv("FM_MACHINE_FILE", str(tmp_path / "absent.json"))
    main(["library", "locations", "--json"])
    assert "\n" not in capsys.readouterr().out.strip()
    main(["library", "locations"])
    assert capsys.readouterr().out.startswith("{\n  ")
