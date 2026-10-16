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


def to_hex(addr) -> str:
    if hasattr(addr, "as_hex"):
        return addr.as_hex
    from genlayer.py.types import Address

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
            if "EthSend" in request:
                r = request["EthSend"]
                book.transfers.append((to_hex(r["address"]), int(r["value"])))
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


def facts(vm, incident=True, in_scope=True, minutes=45, page=PAGE):
    vm.clear_mocks()
    vm.mock_web(r"status\.acme\.example", {"status": 200, "body": page})
    vm.mock_llm(
        LLM,
        json.dumps({"incident_on_date": incident, "in_scope": in_scope, "outage_minutes": minutes, "summary": "API down."}),
    )


def claim(vm, c, who, day=DAY5, url=INCIDENT_URL, at="2030-01-06T12:00:00Z", **kw):
    facts(vm, **kw)
    vm.warp(at)
    vm.sender = who
    return c.file_claim(0, day, url)


# ── Fake Bradbury RPC for settle_payout ─────────────────────────────────────

CONSENSUS_MAIN = "0x0112bf6e83497965a5fdd6dad1e447a6e004271d"
MESSAGES = "0x27872802226532fcd7fea2f00addbffbd3380ade"
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
        from genlayer.py import calldata

        p = c.get_payout(payout_id)
        me = to_hex(self.vm._contract_address).lower()
        tx, fin = self._hash(), self._hash()
        self.receipts[("gen_getTransactionReceipt", tx)] = {
            "sender": (sender or p["sender"]).lower(),
            "recipient": me,
            "status": status,
            "txExecutionResult": result,
            "timestamps": {"Created": p["at"] if at is None else at},
            "txCallData": _rlp([calldata.encode({"method": method, "args": args}), b"\x00"]).hex(),
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
