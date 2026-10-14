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
