"""Integration test against Testnet Bradbury, on the packed code that is deployed:

    python deploy/shrink.py contracts/sla_bond.py build/sla_bond.py --pack
    cd tests/integration && gltest . -v -s --network testnet_bradbury   # config in this folder

A claim needs a finished UTC day of coverage, so the full claim flow is the
recorded Bradbury run in the README; this test covers what fits in one sitting,
plus settling the subscription price payout from its own transfer once the
subscribing transaction finalizes (~30 min on Bradbury).
"""

import json
import time

import pytest
from gltest import get_accounts

from .bradbury import deploy, ok, reverted, settle, view

PRICE = 10**15
SLA = "The demo API is available 99.9% of each month. Any full outage counts; announced maintenance is excluded."


@pytest.mark.integration
def test_service_subscribe_and_claim_guards():
    c = deploy("SlaBond", "sla_bond.py")
    hosts = json.dumps(["slabond.cristhedev.com"])
    ok(c.create_service(args=["Demo API", SLA, hosts, PRICE, 30, PRICE // 1000, 10, PRICE // 2]).transact(value=PRICE))
    customer = get_accounts()[1]
    as_customer = c.connect(customer)
    tx = ok(as_customer.subscribe(args=[0]).transact(value=PRICE))
    sub = view(c, "get_subscription", [0, customer.address])
    assert sub["start"] % 86_400 == 0 and sub["start"] > time.time()
    today = int(time.time()) // 86_400 * 86_400
    # Today has not ended, and coverage starts tomorrow.
    assert reverted(as_customer.file_claim(args=[0, today, "https://slabond.cristhedev.com/demo/x"]).transact())
    # The price payout (id 0) settles from its own transfer, once, after the subscribe tx finalizes.
    assert view(c, "get_payout", [0])["kind"] == "price"
    settle(c, 0, tx)
    # Once the transfers have left, the balance matches what the contract expects to hold.
    assert view(c, "get_accounting")["unaccounted"] == 0
