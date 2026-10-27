"""Serve this host's Cloud Storage coordinator on the fleet's Zenoh fabric.

Desktop reaches the coordinator here instead of over SSH. Two keys, under the
host segment the machine card's name derives (``fm-ws-01`` → ``fm_ws_01``):

``fm/storage/<host>/status``
    Discovery. Answered for a wildcard (``fm/storage/*/status``) under this
    host's own key, the way robot and sim agents answer theirs.
``fm/storage/<host>/request``
    One archive operation per query, answered with the same envelope
    ``fm archive --json`` prints. Never answered for a wildcard. Which callers
    may run what is :mod:`fm_tools.storage_requests`'s rule.

The agent runs as the card's ``storage.account``, so the coordinator's own
account check still holds; the operator a signature names is what the library
records as the actor.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fm_tools.archive_workflow import Refusal, configuration, respond
from fm_tools.storage_requests import MAX_BODY_BYTES, Nonces, Request, decode, refusal, run, verify

KEY_PREFIX = "fm/storage"

# tradeoff: the next two are the third copies of fm-robot-agent's and fm-sim's
# router lookup and bounded lane. fm-sim's env.py says a third copy becomes a
# shared package; that package means moving both agents onto it, which is its
# own change. Kept here, small and identical in behaviour, until it lands.
ROUTER_ENDPOINT_KEY = "FM_ROUTER_ENDPOINT"
DEFAULT_COMMS_ENV = Path("/etc/fm-comms.env")


def router_endpoint() -> str:
    """The router this host connects to: the environment, else ``/etc/fm-comms.env``."""
    endpoint = os.environ.get(ROUTER_ENDPOINT_KEY, "").strip()
    if endpoint:
        return endpoint
    override = os.environ.get("FM_COMMS_ENV_FILE", "").strip()
    path = Path(override).expanduser() if override else DEFAULT_COMMS_ENV
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        key, _, value = line.strip().partition("=")
        if key.strip() == ROUTER_ENDPOINT_KEY and value.strip():
            endpoint = value.strip().strip('"').strip("'")
    if not endpoint:
        raise RuntimeError(f"{ROUTER_ENDPOINT_KEY} is set neither in the environment nor in {path}")
    return endpoint


class Lane:
    """A pool with a ceiling on what it holds; ``submit`` refuses past it."""

    def __init__(self, name: str, workers: int, ceiling: int) -> None:
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self.slots = threading.BoundedSemaphore(ceiling)

    def submit(self, work) -> bool:
        if not self.slots.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                work()
            except Exception:  # noqa: BLE001 - a pool swallows it otherwise, unseen
                print(f"fm-storage-agent: lane worker failed:\n{traceback.format_exc()}", file=sys.stderr, flush=True)
            finally:
                self.slots.release()

        try:
            self.pool.submit(run)
        except Exception:
            self.slots.release()
            raise
        return True

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)


#: tradeoff: reads are quick and many; writes include a copy plan or a scan
#: that probes remote hosts for minutes. Each lane bounds what it holds and
#: answers busy past that, so a slow scan never starves a library page.
READ_LANE = ("read", 4, 16)
WRITE_LANE = ("write", 2, 4)


def key_of(host: str, verb: str) -> str:
    return f"{KEY_PREFIX}/{host}/{verb}"


def encode(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True).encode()


def safe_refusal(exc: Exception) -> dict:
    """The refusal a caller sees: a code of the shape respond() lets through, never a message or a path."""
    code = getattr(exc, "code", None)
    return refusal(code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,80}", code)
                   else "precondition_failed")


class Agent:
    def __init__(self, host: str, name: str) -> None:
        self.host = host
        self.name = name
        self.nonces = Nonces()
        # Requests carry whole seconds. One issued in the second this agent
        # started may predate it, so the first second it accepts is the next.
        self.not_before = int(time.time()) + 1

    def status(self) -> dict:
        return {"contract_version": 1, "ok": True, "host": self.host, "name": self.name}

    def authorise(self, request: Request) -> str | dict:
        """The verified operator, or the refusal to send. Runs before a worker is taken."""
        try:
            # Read on each request, so a key added to or removed from the card
            # takes effect without a restart.
            operators = configuration()[1].get("operators", [])
            return verify(request, host=self.host, operators=operators, nonces=self.nonces,
                          now=time.time(), not_before=self.not_before)
        except Exception as exc:  # noqa: BLE001 - a refusal the caller can read, never a silent drop
            return safe_refusal(exc)

    def run(self, request: Request, actor: str) -> dict:
        try:
            return run(request, actor, respond)
        except Exception as exc:  # noqa: BLE001 - a refusal the caller can read, never a silent drop
            return safe_refusal(exc)


def _reply(query, key: str, body: dict) -> None:
    import zenoh

    if body.get("ok"):
        query.reply(key, encode(body), encoding=zenoh.Encoding.APPLICATION_JSON)
    else:
        query.reply_err(encode(body), encoding=zenoh.Encoding.APPLICATION_JSON)


class Fabric:
    def __init__(self, session, agent: Agent, reply=_reply) -> None:
        self.lanes = {"read": Lane(*READ_LANE), "write": Lane(*WRITE_LANE)}
        status_key, request_key = key_of(agent.host, "status"), key_of(agent.host, "request")

        def status(query) -> None:
            reply(query, status_key, agent.status())

        def handle(query) -> None:
            # A request names one coordinator; a wildcard would reach every one.
            if str(query.key_expr) != request_key:
                return
            # Measured before it is copied out of Zenoh.
            if query.payload is not None and len(query.payload) > MAX_BODY_BYTES:
                reply(query, request_key, refusal("invalid_request"))
                return
            try:
                request = decode(query.payload.to_bytes() if query.payload is not None else None)
            except Refusal as refused:
                reply(query, request_key, refusal(refused.code))
                return
            actor = agent.authorise(request)
            if isinstance(actor, dict):
                reply(query, request_key, actor)
                return
            lane = self.lanes["write" if request.is_write else "read"]
            if not lane.submit(lambda: reply(query, request_key, agent.run(request, actor))):
                reply(query, request_key, refusal("busy"))

        self.queryables = [session.declare_queryable(status_key, status),
                           session.declare_queryable(request_key, handle)]

    def close(self) -> None:
        for lane in self.lanes.values():
            lane.shutdown()
        for queryable in self.queryables:
            queryable.undeclare()


def _session_config(endpoint: str):
    import zenoh

    config = zenoh.Config()
    config.insert_json5("mode", json.dumps("client"))
    config.insert_json5("connect/endpoints", json.dumps([endpoint]))
    return config


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(prog="fm-storage-agent",
                            description="Serve this host's Cloud Storage coordinator over Zenoh.").parse_args(argv)
    try:
        card, config = configuration()
        endpoint = router_endpoint()
    except (Refusal, RuntimeError) as exc:
        print(f"fm-storage-agent: {getattr(exc, 'code', exc)}", file=sys.stderr)
        return 1
    # Nonces live in this process, so a second agent on the host would accept
    # a request the first has already run. Held for the agent's lifetime.
    try:
        lock = open(Path(config["state_dir"]) / "storage-agent.lock", "a")  # noqa: SIM115 - held until exit
    except OSError as exc:
        print(f"fm-storage-agent: cannot open the agent lock in the state directory: {exc.strerror}", file=sys.stderr)
        return 1
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("fm-storage-agent: another agent is serving this host", file=sys.stderr)
        return 1
    import zenoh

    agent = Agent(card.namespace, card.name)
    stop = threading.Event()
    # systemd stops the unit with SIGTERM; leave through the same cleanup as Ctrl-C.
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with zenoh.open(_session_config(endpoint)) as session:
        fabric = Fabric(session, agent)
        print(f"fm-storage-agent: serving {KEY_PREFIX}/{agent.host}/* via {endpoint}", flush=True)
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass
        finally:
            fabric.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
