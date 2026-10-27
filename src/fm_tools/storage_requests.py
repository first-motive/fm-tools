"""Who may ask the storage agent what.

The fabric authenticates no one: anything that reaches the router can query,
including every robot and cloud sim instance. So every request, read or write,
must be signed by an operator key listed on this host's machine card
(``storage.operators``), the fabric's analogue of SSH ``authorized_keys``. Reads
are signed too because they disclose recording contents (file lists and
previews) that only SSH holders could see before. Only the agent's ``status``
key is open, and it names the host alone.

A request body is JSON::

    {"payload": "<base64 of the payload JSON>", "signature": "<base64 DER ECDSA P-256>"}

and the payload names the archive arguments, the host it is for, when it was
issued, a single-use nonce, and the signing key's SHA-256 fingerprint::

    {"argv": ["library", "folder", "create", ...], "host": "fm_ws_01",
     "issued_at": 1793000000, "nonce": "<32 hex>", "key": "<64 hex>"}

The signature covers ``SIGNING_PREFIX`` and the exact payload bytes, so the
client and the agent never have to agree on how JSON is spelled. The host binds
a request to one coordinator, the time and nonce stop a replay, and the key
names the operator the library records as the actor.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fm_tools.archive_workflow import Refusal, operator_key, parser

SIGNING_PREFIX = b"fm-storage-request-v1\n"
REPLY_PREFIX = b"fm-storage-reply-v1\n"
MAX_BODY_BYTES = 64 * 1024
#: How far a request's clock may sit from this host's, either way.
FRESHNESS_S = 120
#: tradeoff: every request claims a nonce, held for twice the freshness window
#: (240 s), so this cap allows about 68 requests a second per coordinator.
#: Past it a request is refused as busy rather than the cache growing without
#: bound. Each claim prunes with one pass over the dict, which is cheap at
#: that size; an expiry-ordered queue is the way out if the cap must rise.
MAX_NONCES = 16384

#: Operations that change nothing: they run on the agent's read lane.
READS = frozenset({
    "library.locations", "library.list", "library.search", "library.show", "library.files",
    "library.preview", "library.history", "copy.show", "jobs.list", "jobs.show", "jobs.status",
})
#: Operations that change the library or start work: they run on the write lane.
WRITES = frozenset({
    "library.refresh", "library.select", "library.folder", "library.item", "library.collection",
    "library.protect", "copy.plan", "copy.start", "copy.verify",
    "jobs.pause", "jobs.resume", "jobs.retry", "jobs.cancel",
})
SERVED = READS | WRITES
# Any listed operator may run any served operation, `copy start` and
# `library protect` included: a key is the authority the coordinator account
# was over SSH, scoped no finer. Remove a leaked key from the card.
# Never served on the fabric: `copy download|export|receiver` are the SSH byte
# paths between hosts, `library recover` replaces the library, and `jobs wait`
# would hold a worker for as long as a caller asks.


def refusal(code: str) -> dict:
    """An archive envelope for a request refused before it ran."""
    return {"contract_version": 1, "operation": None, "request_id": None, "ok": False, "error_code": code}


@dataclass(frozen=True)
class Request:
    """One decoded request, before its signature is checked."""

    payload: dict
    raw: bytes
    signature: bytes
    operation: str

    @property
    def is_write(self) -> bool:
        return self.operation in WRITES


@functools.cache
def served_flags() -> frozenset[str]:
    """The archive flags a request may carry, spelled in full.

    argparse would also take an abbreviation (``--he`` for ``--help``, which
    prints and exits) and ``--flag=value``; a request names each flag exactly,
    and never help or ``--json``, which the agent always implies.
    """
    return frozenset(flag for action in parser()._actions for flag in action.option_strings) - {"-h", "--help", "--json"}


def decode(body: bytes | None) -> Request:
    """The request a body carries, or raise :class:`Refusal`. Checks shape only."""
    if not body or len(body) > MAX_BODY_BYTES:
        raise Refusal("invalid_request")
    try:
        outer = json.loads(body)
        if not isinstance(outer, dict) or set(outer) != {"payload", "signature"}:
            raise Refusal("signature_required" if isinstance(outer, dict) and set(outer) == {"payload"} else "invalid_request")
        raw = base64.b64decode(outer["payload"], validate=True)
        signature = base64.b64decode(outer["signature"], validate=True)
        payload = json.loads(raw)
    except Refusal:
        raise
    except Exception:  # noqa: BLE001 - nested JSON raises RecursionError; any malformed body is one refusal
        raise Refusal("invalid_request") from None
    if not isinstance(payload, dict) or set(payload) != {"argv", "host", "issued_at", "nonce", "key"}:
        raise Refusal("invalid_request")
    argv = payload["argv"]
    if (not isinstance(argv, list) or not 2 <= len(argv) <= 64
            or any(not isinstance(value, str) or len(value) > 1024 for value in argv)):
        raise Refusal("invalid_request")
    flags = served_flags()
    if any(value.startswith("-") and value not in flags for value in argv):
        raise Refusal("invalid_request")
    operation = ".".join(argv[:2])
    if operation not in SERVED:
        raise Refusal("operation_not_served")
    return Request(payload, raw, signature, operation)


class Nonces:
    """Single-use nonces within the freshness window."""

    def __init__(self, capacity: int = MAX_NONCES) -> None:
        self.capacity = capacity
        self.expiry: dict[str, float] = {}
        self.lock = threading.Lock()

    def claim(self, nonce: str, now: float) -> None:
        with self.lock:
            self.expiry = {key: until for key, until in self.expiry.items() if until > now}
            if nonce in self.expiry:
                raise Refusal("request_replayed")
            if len(self.expiry) >= self.capacity:
                raise Refusal("busy")
            self.expiry[nonce] = now + 2 * FRESHNESS_S


def key_id(der: bytes) -> str:
    """The fingerprint a payload names: SHA-256 of the key's DER, in hex."""
    return hashlib.sha256(der).hexdigest()


def verify(request: Request, *, host: str, operators: list[dict], nonces: Nonces, now: float,
           not_before: float = 0) -> str:
    """The operator who signed ``request``, or raise :class:`Refusal` saying why not.

    ``not_before`` is when the agent started: its nonces start empty, so a
    request issued earlier could be a replay it cannot recognise.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    payload = request.payload
    if payload["host"] != host:
        raise Refusal("wrong_host")
    issued = payload["issued_at"]
    if (not isinstance(issued, int) or isinstance(issued, bool) or abs(now - issued) > FRESHNESS_S
            or issued < not_before):
        raise Refusal("request_stale")
    if not isinstance(payload["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", payload["nonce"]):
        raise Refusal("invalid_request")
    # Matched by fingerprint before any key is parsed, so a row that does not
    # decode, or decodes to no point on the curve, fails only requests that
    # name it and never locks out the operators listed after it.
    match = next(((row["name"], der) for row in operators
                  if (der := operator_key(row.get("public_key"))) is not None and key_id(der) == payload["key"]), None)
    if match is None:
        raise Refusal("operator_unknown")
    name, der = match
    try:
        public = load_der_public_key(der)
    except ValueError:
        raise Refusal("operator_unknown") from None
    if not isinstance(public, ec.EllipticCurvePublicKey) or not isinstance(public.curve, ec.SECP256R1):
        raise Refusal("operator_unknown")
    try:
        public.verify(request.signature, SIGNING_PREFIX + request.raw, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise Refusal("signature_invalid") from None
    nonces.claim(payload["nonce"], now)
    return name


def run(request: Request, actor: str, respond: Callable[..., tuple[dict, int]]) -> dict:
    """Run a verified request as ``actor``: only the operation that was signed."""
    envelope, _ = respond(request.payload["argv"], actor=actor, allowed=lambda operation: operation == request.operation)
    return envelope


def answer(request: Request, *, host: str, operators: Callable[[], list[dict]], nonces: Nonces,
           respond: Callable[..., tuple[dict, int]], now: Callable[[], float] = time.time,
           not_before: float = 0) -> dict:
    """Verify then run one request, or say why not.

    Nothing reaches ``respond`` without a verified signature. The agent calls
    :func:`verify` and :func:`run` apart, so a forged request is refused
    before it takes a worker.
    """
    try:
        actor = verify(request, host=host, operators=operators(), nonces=nonces, now=now(),
                       not_before=not_before)
    except Refusal as refused:
        return refusal(refused.code)
    return run(request, actor, respond)


def reply_message(nonce: str, payload: bytes, envelope: bytes) -> bytes:
    """What a reply's signature covers: the request's nonce and the SHA-256 of
    its exact payload bytes, so a reply answers one request and no other."""
    return REPLY_PREFIX + nonce.encode() + b"\n" + hashlib.sha256(payload).hexdigest().encode() + b"\n" + envelope


def seal(envelope: dict, request: Request | None, key) -> bytes:
    """A reply's bytes, signed by the agent's key over its request's nonce.

    The fabric lets any peer answer a query, so a client trusts a reply only
    when the coordinator's own key (published on its card) signed it, and only
    for the request it sent: the nonce and payload digest bind the two. A body
    that never decoded has nothing to bind to and goes back unsigned, which a
    client refuses. Refusals of requests that failed verification are signed
    too, so a client can say why; a peer that saw a request's nonce and payload
    could at most draw such a refusal for it, which only denies that request.
    """
    raw = json.dumps(envelope, sort_keys=True).encode()
    nonce = request.payload.get("nonce") if request is not None else None
    if key is None or not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
        return raw
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    signature = key.sign(reply_message(nonce, request.raw, raw), ec.ECDSA(hashes.SHA256()))
    return json.dumps({"envelope": base64.b64encode(raw).decode(),
                       "signature": base64.b64encode(signature).decode()}, sort_keys=True).encode()
