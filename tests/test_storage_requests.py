"""The storage agent's rule: every request is signed by an operator the card lists."""

from __future__ import annotations

import base64
import json
import secrets

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from fm_tools.storage_requests import SIGNING_PREFIX, Nonces, Refusal, answer, decode, key_id, reply_message, seal

HOST = "fm_ws_01"
NOW = 1_793_000_000


def keypair(name: str = "nish"):
    private = ec.generate_private_key(ec.SECP256R1())
    der = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, {"name": name, "public_key": base64.b64encode(der).decode()}


def body(argv, private, operator, **overrides) -> bytes:
    der = base64.b64decode(operator["public_key"])
    payload = {"argv": argv, "host": HOST, "issued_at": NOW, "nonce": secrets.token_hex(16), "key": key_id(der)}
    raw = json.dumps(payload | overrides).encode()
    signature = private.sign(SIGNING_PREFIX + raw, ec.ECDSA(hashes.SHA256()))
    return json.dumps({"payload": base64.b64encode(raw).decode(),
                       "signature": base64.b64encode(signature).decode()}).encode()


def ask(raw: bytes, operators, calls, nonces=None) -> dict:
    def respond(argv, *, actor, allowed):
        calls.append((argv, actor, [allowed(name) for name in ("library.list", "library.folder")]))
        return {"ok": True, "data": {"actor": actor}}, 0

    try:
        request = decode(raw)
    except Refusal as refused:
        return {"ok": False, "error_code": refused.code}
    return answer(request, host=HOST, operators=lambda: operators, nonces=nonces or Nonces(),
                  respond=respond, now=lambda: NOW)


@pytest.fixture
def calls():
    return []


def test_a_signed_read_runs_as_its_operator_and_only_that_operation(calls):
    private, operator = keypair()
    reply = ask(body(["library", "list", "--limit", "100"], private, operator), [operator], calls)
    assert reply["ok"] and reply["data"]["actor"] == "nish"
    assert calls[0][2] == [True, False]


def test_a_signed_write_runs_as_its_operator(calls):
    private, operator = keypair()
    reply = ask(body(["library", "folder", "create", "--name", "Pilot"], private, operator), [operator], calls)
    assert reply["ok"] and reply["data"]["actor"] == "nish"
    assert calls[0][2] == [False, True]


def test_an_unsigned_read_is_refused(calls):
    raw = json.dumps({"payload": base64.b64encode(json.dumps({"argv": ["library", "list"]}).encode()).decode()})
    assert ask(raw.encode(), [], calls)["error_code"] == "signature_required"
    assert calls == []


@pytest.mark.parametrize("make, code", [
    (lambda p, o: body(["library", "folder", "create"], *keypair()), "operator_unknown"),
    (lambda p, o: body(["library", "folder", "create"], p, o, host="fm_ws_02"), "wrong_host"),
    (lambda p, o: body(["library", "folder", "create"], p, o, issued_at=NOW - 600), "request_stale"),
    (lambda p, o: body(["copy", "receiver", "x"], p, o), "operation_not_served"),
    (lambda p, o: body(["library", "recover", "x"], p, o), "operation_not_served"),
    (lambda p, o: body(["jobs", "wait", "x"], p, o), "operation_not_served"),
    (lambda p, o: body(["library", "list", "--he"], p, o), "invalid_request"),
    (lambda p, o: body(["library", "list", "--json"], p, o), "invalid_request"),
    (lambda p, o: body(["library", "list", "--limit=5"], p, o), "invalid_request"),
    (lambda p, o: b"not json", "invalid_request"),
])
def test_refusals_never_reach_the_archive(make, code, calls):
    private, operator = keypair()
    reply = ask(make(private, operator), [operator], calls)
    assert (reply["ok"], reply["error_code"]) == (False, code)
    assert calls == []


def test_a_tampered_payload_fails_its_signature(calls):
    private, operator = keypair()
    outer = json.loads(body(["library", "folder", "create"], private, operator))
    payload = json.loads(base64.b64decode(outer["payload"]))
    payload["argv"] = ["copy", "start", "plan"]
    outer["payload"] = base64.b64encode(json.dumps(payload).encode()).decode()
    reply = ask(json.dumps(outer).encode(), [operator], calls)
    assert reply["error_code"] == "signature_invalid" and calls == []


def test_a_replayed_request_is_refused(calls):
    private, operator = keypair()
    raw, nonces = body(["copy", "start", "plan"], private, operator), Nonces()
    assert ask(raw, [operator], calls, nonces)["ok"]
    assert ask(raw, [operator], calls, nonces)["error_code"] == "request_replayed"
    assert len(calls) == 1


def test_a_bad_row_never_locks_out_the_operators_after_it(calls):
    private, operator = keypair("matt")
    bad = {"name": "nish", "public_key": "QUJD"}
    assert ask(body(["library", "list"], private, operator), [bad, operator], calls)["data"]["actor"] == "matt"


def card(tmp_path, monkeypatch, operators):
    import getpass

    storage = {"contract_version": 1, "account": getpass.getuser(), "state_dir": str(tmp_path / "state"),
               "locations": []}
    if operators is not None:
        storage["operators"] = operators
    path = tmp_path / "machine.json"
    path.write_text(json.dumps({"schema_version": 1, "name": "fm-ws-01", "role": "workstation", "fleet": "",
                                "transport": "zenoh", "workspace": str(tmp_path), "storage": storage}))
    monkeypatch.setenv("FM_MACHINE_FILE", str(path))


@pytest.mark.parametrize("operators", [None, [], [keypair()[1]]])
def test_the_card_may_list_operators(tmp_path, monkeypatch, operators):
    from fm_tools.archive_workflow import configuration

    card(tmp_path, monkeypatch, operators)
    assert configuration()[1].get("operators", []) == (operators or [])


@pytest.mark.parametrize("operators", [
    "nish",
    [{"name": "Nish Govender", "public_key": keypair()[1]["public_key"]}],
    [{"name": "nish", "public_key": "not base64!"}],
    [{"name": "nish", "public_key": base64.b64encode(b"\0" * 91).decode()}],
    [{"name": "nish", "public_key": keypair()[1]["public_key"], "admin": True}],
    [keypair("nish")[1], keypair("nish")[1]],
])
def test_a_malformed_operator_list_refuses_the_card(tmp_path, monkeypatch, operators):
    from fm_tools.archive_workflow import Refusal, configuration

    card(tmp_path, monkeypatch, operators)
    with pytest.raises(Refusal, match="invalid_operators"):
        configuration()


def test_a_request_from_before_the_agent_started_is_refused(calls):
    private, operator = keypair()
    request = decode(body(["library", "list"], private, operator))
    reply = answer(request, host=HOST, operators=lambda: [operator], nonces=Nonces(),
                   respond=lambda *a, **k: ({"ok": True}, 0), now=lambda: NOW, not_before=NOW + 1)
    assert reply["error_code"] == "request_stale"


def test_deeply_nested_json_is_one_refusal():
    nested = base64.b64encode(b"[" * 50_000).decode()
    with pytest.raises(Refusal, match="invalid_request"):
        decode(json.dumps({"payload": nested, "signature": "AA=="}).encode())


def test_an_off_curve_key_fails_only_requests_that_name_it(calls):
    off_curve = base64.b64encode(bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d03010703420004") + b"\xff" * 64).decode()
    private, operator = keypair("matt")
    bad = {"name": "nish", "public_key": off_curve}
    forged = body(["library", "list"], private, bad)
    assert ask(forged, [bad, operator], calls)["error_code"] == "operator_unknown"
    assert ask(body(["library", "list"], private, operator), [bad, operator], calls)["data"]["actor"] == "matt"


def test_the_data_checkout_joins_the_path_once(tmp_path, monkeypatch):
    import sys

    from fm_tools.archive_workflow import data_package

    root = tmp_path / "fm-data"
    (root / "fm_data_archive" / "fm_data_archive" / "core").mkdir(parents=True)
    (root / "fm_data_archive" / "fm_data_archive" / "core" / "library.py").write_text("")
    monkeypatch.setattr(sys, "path", list(sys.path))
    data_package(tmp_path)
    data_package(tmp_path)
    assert sys.path.count(str(root / "fm_data_archive")) == 1



def test_a_reply_is_sealed_over_its_requests_nonce():
    agent = ec.generate_private_key(ec.SECP256R1())
    private, operator = keypair()
    request = decode(body(["library", "list"], private, operator))
    envelope = {"contract_version": 1, "ok": True, "operation": "library.list", "data": {"revision": 3}}
    sealed = json.loads(seal(envelope, request, agent))
    raw = base64.b64decode(sealed["envelope"])
    assert json.loads(raw) == envelope
    message = reply_message(request.payload["nonce"], request.raw, raw)
    agent.public_key().verify(base64.b64decode(sealed["signature"]), message, ec.ECDSA(hashes.SHA256()))


def test_a_reply_with_nothing_to_bind_goes_unsigned():
    envelope = {"ok": False, "error_code": "invalid_request"}
    assert json.loads(seal(envelope, None, ec.generate_private_key(ec.SECP256R1()))) == envelope
