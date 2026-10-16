"""Helpers for the Bradbury integration test: deploy the packed build, read
views and transaction results straight from the RPC, settle a payout from its
own transfer with the recipient's balance checked in the finalizing block."""

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from genlayer_py.contracts import actions
from genlayer_py.transactions import actions as tx_actions
from genlayer_py.types import transactions as tx_types
from gltest.clients import get_gl_client
from gltest.contracts.contract_factory import ContractFactory

RPC = "https://rpc-bradbury.genlayer.com"
CONSENSUS_MAIN = "0x0112Bf6e83497965A5fdD6Dad1E447a6E004271D"
T_FINALIZED = "0x24196ec40f90671b7144e716978686b4f3c7d6b00b23da3971ef2b52af27d87f"
TIMEOUTS = (12, 13)  # gen statuses for leader / validators timeout: the transaction did not execute

# genlayer_py sends the consensus-layer transaction with exactly eth_estimateGas;
# by inclusion the state has moved and it can run out of gas and revert.
_prepare = actions._prepare_transaction


def _prepare_with_margin(*args, **kwargs):
    tx = _prepare(*args, **kwargs)
    tx["gas"] = hex(int(tx["gas"], 16) * 3 // 2)
    return tx


actions._prepare_transaction = _prepare_with_margin


class _Statuses(dict):
    """Bradbury reports statuses genlayer_py does not know (e.g. 14) while a
    transaction is in flight; treat them as pending instead of crashing the wait."""

    def __missing__(self, key):
        return tx_types.TransactionStatus.PENDING


tx_types.TRANSACTION_STATUS_NUMBER_TO_NAME = tx_actions.TRANSACTION_STATUS_NUMBER_TO_NAME = _Statuses(
    tx_types.TRANSACTION_STATUS_NUMBER_TO_NAME
)


def rpc(method, params):
    req = urllib.request.Request(
        RPC,
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},  # the RPC rejects Python's default agent
    )
    for attempt in range(5):  # the edge returns occasional 5xx over a long run
        try:
            out = json.load(urllib.request.urlopen(req, timeout=60))
            break
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 4:
                raise
            time.sleep(10)
    if "error" in out:
        raise RuntimeError(f"{method}: {out['error']}")
    return out["result"]


def deploy(name, build):
    """Deploy build/<file>; the packed build hides the class inside exec(), so gltest's AST lookup cannot find it."""
    code = (Path(__file__).parents[2] / "build" / build).read_text()
    factory = ContractFactory(contract_name=name, contract_code=code)
    receipt = factory.deploy_contract_tx(args=[])
    ok(receipt)
    c = factory.build_contract(contract_address=receipt["recipient"])
    print("contract", c.address)
    return c


def view(c, fn, args=()):
    """Raw gen_call: genlayer_py's read_contract does not parse Bradbury's response."""
    from genlayer_py.abi import calldata
    from genlayer_py.abi.transactions import serialize
    from genlayer_py.contracts.utils import make_calldata_object

    data = serialize([calldata.encode(make_calldata_object(method=fn, args=list(args), kwargs=None)), b"\x00"])
    r = get_gl_client().provider.make_request(
        method="gen_call",
        params=[{"type": "read", "to": c.address, "from": c.address, "data": data, "transaction_hash_variant": "latest-nonfinal"}],
    )["result"]
    if isinstance(r, dict):
        r = r.get("data") or r.get("result")
    return calldata.decode(bytes.fromhex(r.removeprefix("0x")))


def _result(receipt):
    """(tx id, txExecutionResult) from the chain: gltest's assertions only read Studio receipts."""
    tx = receipt.get("tx_id") or receipt.get("hash")
    return tx, rpc("gen_getTransactionReceipt", [{"txId": tx}])["txExecutionResult"]


def ok(receipt):
    tx, res = _result(receipt)
    assert res == 1, f"{tx} execution result {res}"
    print("  ok", tx)
    return tx


def reverted(receipt):
    tx, res = _result(receipt)
    print("  reverted" if res == 2 else "  NOT reverted", tx)
    return res == 2


def send(make):
    """make() sends a transaction; resend while the leader or validators time out (nothing executed), then require success."""
    for _ in range(3):
        receipt = make()
        tx, res = _result(receipt)
        if res or rpc("gen_getTransactionReceipt", [{"txId": tx}])["status"] not in TIMEOUTS:
            break
        print("  leader/validators timed out, nothing executed; sending again:", tx)
    return ok(receipt)


def balance(addr, block):
    return int(rpc("eth_getBalance", [addr, hex(block)]), 16)


def finalization(tx, timeout=3 * 3600):
    """Wait until GenLayer tx `tx` is finalized; return the consensus-layer tx that finalized it and its block."""
    deadline = time.time() + timeout
    while True:
        r = rpc("gen_getTransactionReceipt", [{"txId": tx}])
        if r and r.get("status") == 7:
            break
        assert time.time() < deadline, f"{tx} not finalized: {r and r.get('status')}"
        time.sleep(30)
    start = int(r["readStateBlockRanges"][0]["ProposalBlock"])
    head = int(rpc("eth_blockNumber", []), 16)
    for lo in range(start, head + 1, 10_000):
        logs = rpc(
            "eth_getLogs",
            [{"address": CONSENSUS_MAIN, "topics": [T_FINALIZED, tx], "fromBlock": hex(lo), "toBlock": hex(min(lo + 9_999, head))}],
        )
        if logs:
            return logs[0]["transactionHash"], int(logs[0]["blockNumber"], 16)
    raise AssertionError(f"no TransactionFinalized log for {tx}")


def settle(c, payout_id, tx):
    """Settle payout `payout_id`, paid by GenLayer tx `tx`, as DELIVERED; once only."""
    p = view(c, "get_payout", [payout_id])
    fin, block = finalization(tx)
    moved = balance(p["to"], block) - balance(p["to"], block - 1)
    assert moved == p["amount"], (p, fin, moved)
    send(lambda: c.settle_payout(args=[payout_id, tx, fin]).transact(wait_interval=10_000, wait_retries=60))
    got = view(c, "get_payout", [payout_id])
    assert got["status"] == "DELIVERED" and got["tx"] == tx.lower(), got
    print(f"payout {payout_id} {p['kind']} -> {p['to']}: DELIVERED  paid {tx}  finalized {fin} (block {block}, +{moved} wei)")
    assert reverted(c.settle_payout(args=[payout_id, tx, fin]).transact())
