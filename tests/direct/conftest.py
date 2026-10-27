"""Fixtures for SLA Bond direct-mode tests."""

import json

import pytest

GEN = 10**18
DAY = 86_400
PRICE = GEN
MAX_CREDIT = GEN // 2
CPM = GEN // 100  # credit per outage minute
BOND = 2 * MAX_CREDIT  # room for exactly two customers
SLA = (
    "The public API (api.acme.example) is available 99.9% of each month. Any full outage or error rate "
    "above 5% counts. Excluded: maintenance announced at least 48 hours in advance."
)
HOSTS = json.dumps(["status.acme.example"])
INCIDENT_URL = "https://status.acme.example/incidents/2030-01-05"
DAY5 = 1893801600  # 2030-01-05 00:00 UTC
PAGE = "Jan 5, 2030 - API outage. 14:10-14:55 UTC all API requests failed. Resolved."
LLM = r"extract facts from a service provider"


# gltest 0.29 targets the pre-v0.3 SDK (`genlayer.py`, `genlayer.gl`). Bridge it to the
# v0.3 runner this contract declares: alias the moved modules, and replace the four
# hooks that reach into removed internals (storage allocation, run_nondet, message, validator).
import os
import sys
import types

import gltest.direct.loader as _loader
import gltest.direct.sdk_loader as _sdk
from gltest.direct.vm import VMContext

def _v03_runner():
    """Extract the runner the contract header names (via genvm-lint's bundle cache) into
    gltest's cache layout; returns the runner's cloudpickle source dir."""
    from pathlib import Path

    from genvm_linter.validate import artifacts as A

    runner = _sdk.parse_contract_header(Path(__file__).parents[2] / "contracts/sla_bond.py")["py-genlayer"]
    version = A.resolve_version()
    bundle = A.download_artifacts(version)
    rdir = A.extract_runner(bundle, "py-genlayer", runner)
    deps = A.parse_runner_manifest(rdir)
    A.extract_runner(bundle, "py-lib-genlayer-std", deps["py-lib-genlayer-std"])
    pickle_dir = A.extract_runner(bundle, "py-lib-cloudpickle", deps["py-lib-cloudpickle"])
    tag = "glint-" + version.rsplit("-v", 1)[-1]
    (_sdk.CACHE_DIR / "extracted").mkdir(parents=True, exist_ok=True)
    for link, target in ((_sdk.CACHE_DIR / f"genvm-universal-{tag}.tar.xz", bundle), (_sdk.CACHE_DIR / "extracted" / tag, rdir.parents[1])):
        if not link.exists():
            link.symlink_to(target)
    os.environ.setdefault("GENVM_VERSION", tag)
    return pickle_dir / "src"

# run_nondet pickles its blocks; the v0.3 runner ships cloudpickle as its own library.
sys.path.append(str(_v03_runner()))

_setup_paths = _sdk.setup_sdk_paths

def _setup_and_alias(*a, **kw):
    added = _setup_paths(*a, **kw)
    import genlayer.calldata
    import genlayer.types

    py = types.ModuleType("genlayer.py")
    py.calldata, py.types = genlayer.calldata, genlayer.types
    sys.modules.update({"genlayer.py": py, "genlayer.py.calldata": py.calldata, "genlayer.py.types": py.types})
    return added

_sdk.setup_sdk_paths = _setup_and_alias

def _allocate(cls, vm, *args, **kwargs):
    from genlayer.storage import ROOT_SLOT_ID
    from genlayer.storage._internal.generate import ORIGINAL_INIT_ATTR, _BuilderCtx, _storage_build

    td = _storage_build(_BuilderCtx.empty(), cls)
    instance = td.get(vm._storage.get_store_slot(ROOT_SLOT_ID), 0)
    init = getattr(getattr(td, "cls", cls), "__init__")
    getattr(init, ORIGINAL_INIT_ATTR, init)(instance, *args, **kwargs)
    return instance

def _patch_run_nondet():
    import genlayer.vm as gl_vm
    from genlayer.types import Lazy
    from gltest.direct import wasi_mock

    def run_nondet(leader_fn, validator_fn, /, **_):
        vm = wasi_mock.get_vm()
        vm._in_nondet = True
        try:
            result = leader_fn()
        finally:
            vm._in_nondet = False
        vm._captured_validators.append((result, leader_fn, validator_fn))
        return result

    run_nondet.lazy = lambda l, v, /, **kw: Lazy(lambda: run_nondet(l, v, **kw))
    gl_vm.run_nondet = run_nondet

def _refresh_message(self):
    msg = sys.modules.get("genlayer.message")
    if msg is None:
        return
    from genlayer.types import Address

    a = lambda x: Address(x) if isinstance(x, bytes) else x
    upd = {"sender_address": a(self.sender), "origin_address": a(self.origin), "datetime": self._datetime, "value": self._value}
    msg.raw.update(upd)
    vars(msg).update(upd)

_run_validator = VMContext.run_validator

def _validator(self, *, leader_result=_loader.__dict__.get("_sentinel", ...), leader_error=None, index=-1):
    import genlayer.vm as gl_vm

    stored, _, validator_fn = self._captured_validators[index]
    if leader_error is not None:
        return validator_fn(gl_vm.UserError(str(leader_error)))
    return validator_fn(gl_vm.Return(stored if leader_result is ... else leader_result))

import json as _json

from gltest.direct import wasi_mock as _wasi

_llm = _wasi._handle_llm_request

def _llm_text(vm, data):
    # v0.3 exec_prompt parses JSON itself and expects the raw model text.
    r = _llm(vm, data)
    if isinstance(r, dict) and "ok" in r and not isinstance(r["ok"], str):
        return {"ok": _json.dumps(r["ok"])}
    return r

_wasi._handle_llm_request = _llm_text
_loader._allocate_contract = _allocate
_loader._patch_run_nondet_for_direct_mode = _patch_run_nondet
VMContext._refresh_gl_message = _refresh_message
VMContext.run_validator = _validator

def to_hex(addr) -> str:
    if hasattr(addr, "as_hex"):
        return addr.as_hex
    from genlayer.types import Address

    return Address(addr).as_hex


class Ledger:
    def __init__(self):
        self.transfers = []
        self.events = []

    def paid_to(self, addr) -> int:
        h = (addr if isinstance(addr, str) else to_hex(addr)).lower()
        return sum(v for a, v in self.transfers if a.lower() == h)


@pytest.fixture
def ledger():
    import gltest.direct.wasi_mock as wm

    book = Ledger()
    original = wm._handle_gl_call

    def spy(vm, request):
        if isinstance(request, dict):
            if "EmitExternalMessage" in request:
                r = request["EmitExternalMessage"]
                book.transfers.append((to_hex(r["address"]), int(r["value"])))
                return {"ok": None}
            if "EmitEvent" in request:
                book.events.append(request["EmitEvent"]["blob"])
        return original(vm, request)

    wm._handle_gl_call = spy
    yield book
    wm._handle_gl_call = original


def iso(ts: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def chain_balance(vm, amount):
    """The mock never moves value, so tests set the contract's balance as the chain would show it."""
    vm.deal(vm._contract_address, amount)


@pytest.fixture
def sla(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """Service 0 by Alice; Bob and Charlie subscribe on Jan 1 (covered from Jan 2)."""
    direct_vm.warp("2030-01-01T10:00:00Z")
    c = direct_deploy("contracts/sla_bond.py")
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    c.create_service("Acme API", SLA, HOSTS, PRICE, 30, CPM, 10, MAX_CREDIT)
    for who in (direct_bob, direct_charlie):
        direct_vm.sender = who
        direct_vm.value = PRICE
        chain_balance(direct_vm, BOND + PRICE)
        c.subscribe(0)
    direct_vm.value = 0
    chain_balance(direct_vm, BOND)  # both prices went to the provider
    return c


def facts(vm, incident=True, in_scope=True, minutes=45, page=PAGE, evidence=PAGE[:40]):
    vm.clear_mocks()
    vm.mock_web(r"status\.acme\.example", {"status": 200, "body": page})
    vm.mock_llm(
        LLM,
        json.dumps(
            {"incident_on_date": incident, "in_scope": in_scope, "outage_minutes": minutes, "evidence": evidence, "summary": "API down."}
        ),
    )


def claim(vm, c, who, day=DAY5, url=INCIDENT_URL, at="2030-01-06T12:00:00Z", **kw):
    facts(vm, **kw)
    vm.warp(at)
    vm.sender = who
    return c.file_claim(0, day, url)


# ── Fake Bradbury RPC for settle_payout ─────────────────────────────────────

CONSENSUS_MAIN = "0x2e2766cbc3e5e61ce93bbcdcbb3eacb82082a019"
MESSAGES = "0xd14b0489157fe420555f1e5a6afba031e978ace5"
T_FINALIZED = "0x24196ec40f90671b7144e716978686b4f3c7d6b00b23da3971ef2b52af27d87f"
T_EXECUTED = "0x4aacc1769103d0575734a31601a30d8f74c7843414f868342af438b4fdc4d78e"
T_MESSAGE = "0x1a2b93b175bf31d44b127b5c4c0f72cbf128f5230ad594a52a00dfa559c96b51"
T_MSG_FAILED = "0x5ae8e754fbc8b7b56c89e264bdcd8bb847036b23ac1a0f791f48b62438f2bab9"


def _rlp(item) -> bytes:
    def prefix(n, off):
        if n < 56:
            return bytes([off + n])
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return bytes([off + 55 + len(b)]) + b

    if isinstance(item, list):
        body = b"".join(_rlp(x) for x in item)
        return prefix(len(body), 0xC0) + body
    return item if len(item) == 1 and item[0] < 0x80 else prefix(len(item), 0x80) + item


def _pad(addr: str) -> str:
    return "0x" + addr.lower()[2:].rjust(64, "0")


class Chain:
    """Serves gen_getTransactionReceipt / eth_getTransactionReceipt like Bradbury,
    for paying transactions registered with `pay`."""

    def __init__(self, vm):
        self.vm = vm
        self.receipts = {}
        self.n = 0
        self.down = False

    def _hash(self):
        self.n += 1
        return "0x" + f"{self.n:064x}"

    def pay(self, c, payout_id, method, args, delivered=True, status=7, result=1, sender=None, at=None):
        """Record the paying transaction of `payout_id` and its finalization. Returns (tx, finalize_tx)."""
        from genlayer import calldata

        p = c.get_payout(payout_id)
        me = to_hex(self.vm._contract_address).lower()
        tx, fin = self._hash(), self._hash()
        self.receipts[("gen_getTransactionReceipt", tx)] = {
            "sender": (sender or p["sender"]).lower(),
            "recipient": me,
            "status": status,
            "txExecutionResult": result,
            "timestamps": {"Created": p["at"] if at is None else at},
            # genlayer-js 2.x encodes the method name under "".
            "txCallData": _rlp([calldata.encode({"": method, "args": args}), b"\x00"]).hex(),
        }
        msg = "0x" + f"{self.n:064x}"
        logs = [
            {"address": CONSENSUS_MAIN, "topics": [T_FINALIZED, tx], "data": "0x"},
        ]
        if delivered:  # True: delivered; False: the transfer failed; None: the transaction sent nothing
            logs.append({"address": MESSAGES, "topics": [T_MESSAGE, tx, msg], "data": "0x"})
            logs.append({"address": me, "topics": [T_EXECUTED, _pad(p["to"])], "data": msg + "00" * 64})
        elif delivered is False:  # as on Bradbury: the messages contract's failure event, no MessagesOnFinalization
            logs.append({"address": MESSAGES, "topics": [T_MSG_FAILED, tx, msg], "data": "0x"})
        self.receipts[("eth_getTransactionReceipt", fin)] = {"status": "0x1", "to": CONSENSUS_MAIN, "logs": logs}
        return tx, fin

    def answer(self, data):
        body = data.get("body") or b""
        req = json.loads(body.decode() if isinstance(body, (bytes, bytearray)) else body)
        if self.down:
            return {"ok": {"response": {"status": 502, "headers": {}, "body": b"bad gateway"}}}
        params = req["params"][0]
        key = params["txId"] if isinstance(params, dict) else params
        out = {"jsonrpc": "2.0", "id": req["id"], "result": self.receipts.get((req["method"], key))}
        return {"ok": {"response": {"status": 200, "headers": {}, "body": json.dumps(out).encode()}}}


@pytest.fixture
def chain(direct_vm):
    import gltest.direct.wasi_mock as wm

    c = Chain(direct_vm)
    original = wm._handle_web_request

    def route(vm, data):
        if "rpc-bradbury" in data.get("url", ""):
            return c.answer(data)
        return original(vm, data)

    wm._handle_web_request = route
    yield c
    wm._handle_web_request = original
