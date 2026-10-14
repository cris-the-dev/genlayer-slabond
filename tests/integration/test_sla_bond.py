"""Integration test against Testnet Bradbury, on the packed code that is deployed:

    python deploy/shrink.py contracts/sla_bond.py build/sla_bond.py --pack
    cd tests/integration && gltest . -v -s --network testnet_bradbury   # config in this folder

A claim needs a finished UTC day of coverage, so the full claim flow is the
recorded Bradbury run in the README; this test covers what fits in one sitting.
"""

import json
import time

import pytest
from gltest import get_accounts, get_contract_factory
from gltest.assertions import tx_execution_failed, tx_execution_succeeded

PRICE = 10**15
SLA = "The demo API is available 99.9% of each month. Any full outage counts; announced maintenance is excluded."


@pytest.mark.integration
def test_service_subscribe_and_claim_guards():
    c = get_contract_factory("SlaBond").deploy(args=[])
    hosts = json.dumps(["slabond.cristhedev.com"])
    assert tx_execution_succeeded(
        c.create_service(args=["Demo API", SLA, hosts, PRICE, 30, PRICE // 1000, 10, PRICE // 2]).transact(value=PRICE)
    )
    customer = c.connect(get_accounts()[1])
    assert tx_execution_succeeded(customer.subscribe(args=[0]).transact(value=PRICE))
    sub = c.get_subscription(args=[0, get_accounts()[1].address]).call()
    assert sub["start"] % 86_400 == 0 and sub["start"] > time.time()
    today = int(time.time()) // 86_400 * 86_400
    # Today has not ended, and coverage starts tomorrow.
    assert tx_execution_failed(customer.file_claim(args=[0, today, "https://slabond.cristhedev.com/demo/x"]).transact())
    assert c.get_accounting(args=[]).call()["unaccounted"] == 0
