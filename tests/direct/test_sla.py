"""Service lifecycle: subscriptions, claims, credits, bond release."""

import pytest

from tests.direct.conftest import (
    BOND,
    CPM,
    DAY,
    DAY5,
    GEN,
    HOSTS,
    MAX_CREDIT,
    PRICE,
    SLA,
    chain_balance,
    claim,
    iso,
    to_hex,
)


def test_create_validation(direct_vm, direct_deploy, direct_alice):
    c = direct_deploy("contracts/sla_bond.py")
    direct_vm.sender = direct_alice
    direct_vm.value = BOND
    ok = ["Acme API", SLA, HOSTS, PRICE, 30, CPM, 10, MAX_CREDIT]
    for i, bad, msg in [
        (1, "short", "sla must be"),
        (2, '["https://x.com/"]', "invalid hostname"),
        (2, "[]", "1-5 hostnames"),
        (4, 0, "term 1-365"),
        (7, PRICE + 1, "max_credit must be 1..price"),
    ]:
        args = list(ok)
        args[i] = bad
        with direct_vm.expect_revert(msg):
            c.create_service(*args)
    direct_vm.value = MAX_CREDIT - 1
    with direct_vm.expect_revert("bond must cover"):
        c.create_service(*ok)


def test_subscription_starts_next_midnight(sla, direct_bob):
    s = sla.get_service(0)
    assert s["live_subscribers"] == 2 and s["bond"] == BOND
    sub = sla.get_subscription(0, to_hex(direct_bob))
    assert sub["start"] == DAY5 - 3 * DAY and sub["end"] == sub["start"] + 30 * DAY
    assert sla.get_accounting()["unaccounted"] == 0


def test_subscribe_rules(sla, direct_vm, direct_alice, direct_bob, direct_owner, ledger):
    direct_vm.sender = direct_owner
    direct_vm.value = PRICE - 1
    with direct_vm.expect_revert("exactly the price"):
        sla.subscribe(0)
    direct_vm.value = PRICE
    chain_balance(direct_vm, BOND + PRICE)
    with direct_vm.expect_revert("bond too small"):
        sla.subscribe(0)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("provider cannot subscribe"):
        sla.subscribe(0)
    # Renewing needs no extra bond and extends the same period.
    direct_vm.sender = direct_bob
    before = sla.get_subscription(0, to_hex(direct_bob))
    direct_vm.warp("2030-01-01T10:00:05Z")  # not the second of Bob's first payment
    sla.subscribe(0)
    after = sla.get_subscription(0, to_hex(direct_bob))
    assert after["start"] == before["start"] and after["end"] == before["end"] + 30 * DAY
    assert ledger.paid_to(direct_alice) == PRICE


def test_lapsed_customer_cannot_drop_claimable_days(sla, direct_vm, direct_bob):
    end = sla.get_subscription(0, to_hex(direct_bob))["end"]
    direct_vm.warp(iso(end + DAY))
    direct_vm.sender = direct_bob
    direct_vm.value = PRICE
    chain_balance(direct_vm, BOND + PRICE)
    with direct_vm.expect_revert("after its claim window"):
        sla.subscribe(0)
    direct_vm.warp(iso(end + 8 * DAY))
    assert sla.subscribe(0)["start"] == end + 9 * DAY


def test_approved_claim_credits_every_covered_customer(sla, direct_vm, direct_bob, direct_charlie, ledger):
    r = claim(direct_vm, sla, direct_bob, minutes=45)
    assert r["decision"] == "approved" and r["outage_minutes"] == 45
    assert r["credit"] == 45 * CPM  # below the 0.5 GEN max
    c = sla.get_claims(0)[0]
    assert c["covered"] == 2 and c["reserved"] == 90 * CPM and c["date"] == "2030-01-05"
    assert sla.get_service(0)["bond"] == BOND - 90 * CPM
    for who in (direct_bob, direct_charlie):
        direct_vm.sender = who
        assert sla.credit_status(0, to_hex(who)) == "available"
        assert sla.withdraw_credit(0) == 45 * CPM
        assert ledger.paid_to(who) == 45 * CPM
        with direct_vm.expect_revert("already withdrawn"):
            sla.withdraw_credit(0)
    assert sla.get_claims(0)[0]["reserved"] == 0


def test_credit_is_capped_and_one_claim_per_day(sla, direct_vm, direct_bob, direct_charlie):
    assert claim(direct_vm, sla, direct_bob, minutes=600)["credit"] == MAX_CREDIT
    with direct_vm.expect_revert("already claimed"):
        claim(direct_vm, sla, direct_charlie)


def test_rejected_claim_does_not_lock_the_day(sla, direct_vm, direct_bob, direct_charlie):
    r = claim(direct_vm, sla, direct_bob, url="https://status.acme.example/incidents/old", incident=False, minutes=0)
    assert r["decision"] == "rejected" and r["credit"] == 0
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("no credit"):
        sla.withdraw_credit(0)
    assert claim(direct_vm, sla, direct_charlie)["decision"] == "approved"


@pytest.mark.parametrize(
    "kw, reason",
    [({"in_scope": False}, "outside the SLA"), ({"minutes": 9}, "below the 10-minute threshold")],
)
def test_rejections(sla, direct_vm, direct_bob, kw, reason):
    r = claim(direct_vm, sla, direct_bob, **kw)
    assert r["decision"] == "rejected" and reason in r["reason"]


def test_out_of_scope_minutes_are_not_recorded(sla, direct_vm, direct_bob):
    claim(direct_vm, sla, direct_bob, in_scope=False, minutes=300)
    assert sla.get_claims(0)[0]["outage_minutes"] == 0


@pytest.mark.parametrize(
    "kw, msg",
    [
        ({"day": DAY5 + 3600}, "00:00 UTC"),
        ({"at": "2030-01-05T23:59:59Z"}, "has not ended"),
        ({"at": "2030-01-13T00:00:01Z"}, "claim window closed"),
        ({"day": DAY5 - 4 * DAY, "at": "2030-01-02T00:00:00Z"}, "not covered"),  # Jan 1: before coverage
        ({"url": "https://attacker.example/acme-outage"}, "provider's hosts"),
        ({"url": "https://status.acme.example.evil.io/x"}, "provider's hosts"),
        ({"url": "http://status.acme.example/x"}, "provider's hosts"),
    ],
)
def test_claim_guards(sla, direct_vm, direct_bob, kw, msg):
    with direct_vm.expect_revert(msg):
        claim(direct_vm, sla, direct_bob, **kw)


def test_subdomain_of_a_listed_host_is_allowed(sla, direct_vm, direct_bob):
    assert claim(direct_vm, sla, direct_bob, url="https://eu.status.acme.example/i/1")["decision"] == "approved"


def test_outsider_cannot_claim_or_withdraw(sla, direct_vm, direct_bob, direct_owner):
    with direct_vm.expect_revert("not covered"):
        claim(direct_vm, sla, direct_owner)
    claim(direct_vm, sla, direct_bob)
    direct_vm.sender = direct_owner
    with direct_vm.expect_revert("not covered"):
        sla.withdraw_credit(0)


def test_credits_go_pro_rata_when_the_bond_runs_short(sla, direct_vm, direct_alice, direct_bob):
    claim(direct_vm, sla, direct_bob, minutes=600)  # reserves 2 x 0.5 GEN: the whole bond
    r = claim(direct_vm, sla, direct_bob, day=DAY5 + DAY, at="2030-01-07T12:00:00Z", minutes=600)
    assert r["decision"] == "approved" and r["credit"] == 0
    direct_vm.sender = direct_alice
    direct_vm.value = GEN // 10
    sla.top_up(0)
    direct_vm.value = 0
    r = claim(direct_vm, sla, direct_bob, day=DAY5 + 2 * DAY, at="2030-01-08T12:00:00Z", minutes=600)
    assert r["credit"] == GEN // 20  # 0.1 GEN split over 2 customers


def test_bond_release_and_sweep(sla, direct_vm, direct_alice, direct_bob, ledger):
    claim(direct_vm, sla, direct_bob, minutes=45)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("close the service first"):
        sla.withdraw_bond(0)
    sla.close_service(0)
    direct_vm.sender = direct_bob
    direct_vm.value = PRICE
    with direct_vm.expect_revert("service is closed"):
        sla.subscribe(0)
    direct_vm.value = 0
    direct_vm.sender = direct_alice
    end = sla.get_service(0)["bond_withdrawable_after"]
    direct_vm.warp(iso(end))
    with direct_vm.expect_revert("claim window still open"):
        sla.withdraw_bond(0)
    with direct_vm.expect_revert("can still be withdrawn"):
        sla.sweep_claim(0)
    direct_vm.warp(iso(end + 90 * DAY))
    assert sla.sweep_claim(0) == 90 * CPM
    assert sla.withdraw_bond(0) == BOND
    assert ledger.paid_to(direct_alice) == BOND
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("expired"):
        sla.withdraw_credit(0)


# ── Each payout settles from its own transfer ───────────────────────────────


def _credit(vm, sla, who):
    claim(vm, sla, who)
    vm.sender = who
    sla.withdraw_credit(0)
    return sla.get_accounting()["payout_count"] - 1


def test_delivered_credit_settles(sla, direct_vm, direct_bob, direct_charlie, chain):
    pid = _credit(direct_vm, sla, direct_bob)
    tx, fin = chain.pay(sla, pid, "withdraw_credit", [0])
    direct_vm.sender = direct_charlie
    assert sla.settle_payout(pid, tx, fin) == "DELIVERED"
    assert sla.get_payout(pid)["tx"] == tx
    assert sla.get_recoverable(to_hex(direct_bob)) == 0
    assert direct_vm.run_validator(leader_result="failed") is False
    with direct_vm.expect_revert("already settled"):
        sla.settle_payout(pid, tx, fin)


def test_failed_credit_is_recoverable_and_a_failed_recovery_again(sla, direct_vm, direct_bob, direct_charlie, chain, ledger):
    pid = _credit(direct_vm, sla, direct_bob)
    tx, fin = chain.pay(sla, pid, "withdraw_credit", [0], delivered=False)
    direct_vm.sender = direct_charlie
    assert sla.settle_payout(pid, tx, fin) == "FAILED"
    with direct_vm.expect_revert("nothing to recover"):
        sla.recover(to_hex(direct_charlie))
    direct_vm.sender = direct_bob
    assert sla.get_recoverable(to_hex(direct_bob)) == 45 * CPM
    direct_vm.warp("2030-01-06T12:00:01Z")
    other = to_hex(direct_charlie)
    assert sla.recover(other) == 45 * CPM
    assert ledger.paid_to(direct_charlie) == 45 * CPM
    # The recovery is a payout too; if it fails it is credited to the recoverer, not the destination.
    rid = sla.get_accounting()["payout_count"] - 1
    assert sla.get_payout(rid)["kind"] == "recover"
    tx2, fin2 = chain.pay(sla, rid, "recover", [other], delivered=False)
    assert sla.settle_payout(rid, tx2, fin2) == "FAILED"
    assert sla.get_recoverable(to_hex(direct_bob)) == 45 * CPM
    assert sla.get_recoverable(other) == 0


def test_failed_price_and_bond_payouts_are_recoverable_by_the_provider(sla, direct_vm, direct_alice, chain):
    chain.n = 100
    tx, fin = chain.pay(sla, 0, "subscribe", [0], delivered=False)  # Bob's subscription price
    assert sla.get_payout(0)["kind"] == "price"
    assert sla.settle_payout(0, tx, fin) == "FAILED"
    assert sla.get_recoverable(to_hex(direct_alice)) == PRICE
    assert sla.get_accounting()["expected_balance"] == BOND + PRICE


@pytest.mark.parametrize(
    "kw,err",
    [
        ({"method": "withdraw_bond", "args": [0]}, "did not create this payout"),
        ({"args": [1]}, "did not create this payout"),
        ({"args": []}, "did not create this payout"),
        ({"sender": "0x" + "99" * 20}, "did not create this payout"),
        ({"at": 1}, "did not create this payout"),
        ({"result": 2}, "did not create this payout"),
        ({"status": 5}, "not finalized yet"),
        # same sender, second, method and id, but it sent nothing (a call that did not pay)
        ({"delivered": None}, "did not create this payout"),
    ],
)
def test_settle_rejects_a_transaction_that_is_not_this_payout(kw, err, sla, direct_vm, direct_bob, chain):
    pid = _credit(direct_vm, sla, direct_bob)
    method, args = kw.pop("method", "withdraw_credit"), kw.pop("args", [0])
    tx, fin = chain.pay(sla, pid, method, args, **{"delivered": False, **kw})
    with direct_vm.expect_revert(err):
        sla.settle_payout(pid, tx, fin)
    assert sla.get_payout(pid)["status"] == "SENT"


def test_a_recover_tx_must_name_the_destination(sla, direct_vm, direct_bob, direct_charlie, chain):
    pid = _credit(direct_vm, sla, direct_bob)
    tx, fin = chain.pay(sla, pid, "withdraw_credit", [0], delivered=False)
    sla.settle_payout(pid, tx, fin)
    direct_vm.warp("2030-01-06T12:00:01Z")
    sla.recover(to_hex(direct_charlie))
    rid = sla.get_accounting()["payout_count"] - 1
    tx2, fin2 = chain.pay(sla, rid, "recover", [to_hex(direct_bob)], delivered=False)
    with direct_vm.expect_revert("did not create this payout"):
        sla.settle_payout(rid, tx2, fin2)


def test_one_paying_tx_settles_one_payout(sla, direct_vm, direct_bob, chain):
    pid = _credit(direct_vm, sla, direct_bob)
    tx, fin = chain.pay(sla, pid, "withdraw_credit", [0])
    sla.settle_payout(pid, tx, fin)
    with direct_vm.expect_revert("already settled a payout"):
        sla.settle_payout(0, tx, fin)


def test_settle_needs_the_finalization_of_that_transaction(sla, direct_vm, direct_bob, chain):
    pid = _credit(direct_vm, sla, direct_bob)
    tx, _ = chain.pay(sla, pid, "withdraw_credit", [0], delivered=False)
    _, other_fin = chain.pay(sla, pid, "withdraw_credit", [0])
    with direct_vm.expect_revert("not the finalization"):
        sla.settle_payout(pid, tx, other_fin)
    with direct_vm.expect_revert("not found"):
        sla.settle_payout(pid, tx, "0x" + "ab" * 32)
    with direct_vm.expect_revert("0x + 64 hex"):
        sla.settle_payout(pid, tx, "latest")
    chain.down = True
    with direct_vm.expect_revert("RPC unavailable"):
        sla.settle_payout(pid, tx, other_fin)


def test_one_payment_per_sender_per_second(sla, direct_vm, direct_bob, chain):
    pid = _credit(direct_vm, sla, direct_bob)
    tx, fin = chain.pay(sla, pid, "withdraw_credit", [0], delivered=False)
    sla.settle_payout(pid, tx, fin)
    # same second, same sender: a second payout is refused and nothing is recorded
    with direct_vm.expect_revert("one payment per sender per second"):
        sla.recover(to_hex(direct_bob))
