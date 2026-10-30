// End-to-end on Testnet Bradbury (Phase 2), on the packed code that is deployed:
//
//   python deploy/shrink.py contracts/sla_bond.py build/sla_bond.py --pack
//   npm i genlayer-js@2.0.0-rc.2   # plus the estimateTransactionFees patch in app/README.md
//   GL_PK0=0x... GL_PK1=0x... node tests/integration/bradbury.mjs build/sla_bond.py [contract]
//   GL_PK0=0x... GL_PK1=0x... node tests/integration/bradbury.mjs claims <contract>
//   GL_PK0=0x... GL_PK1=0x... node tests/integration/bradbury.mjs settle <contract> <payout_id> <paying_tx>
//
// The first run deploys (unless given a contract), opens a service (GL_PK0 is the provider),
// subscribes GL_PK1, checks the guards, and settles the price payout from its own transfer once
// the subscribing transaction finalizes. A claim needs a finished covered UTC day, so `claims`
// runs later on the same contract: coverage starts at the 00:00 UTC after subscribing; the demo
// status pages under slabond.cristhedev.com/demo/ describe the first two covered days.
// Every transaction carries a fee deposit; a paying one also allocates its payout's gas.
// Prints each tx hash and its result; exits non-zero on the first failed expectation.
import { readFileSync } from "node:fs";
import { createAccount, createClient, MessageType, encodeExternalMessageFeeParams } from "genlayer-js";
import { testnetBradbury } from "genlayer-js/chains";

const DAY = 86400;
const PRICE = 10n ** 15n;
const SLA = "The demo API is available 99.9% of each month. Any full outage counts; announced maintenance is excluded.";
const DEMO = "https://slabond.cristhedev.com/demo/";
const CONSENSUS_MAIN = "0x2e2766cbc3e5e61ce93bbcdcbb3eacb82082a019";
const T_FINALIZED = "0x24196ec40f90671b7144e716978686b4f3c7d6b00b23da3971ef2b52af27d87f";
const PAYOUT_GAS = 100000n, PAYOUT_GAS_PRICE = 1000000000n;
// rpc.testnet-chain.genlayer.com (the chain default) does not serve gen_call; this one serves the same chain.
const endpoint = "https://rpc-bradbury.genlayer.com";
const a0 = createAccount(process.env.GL_PK0);
const a1 = createAccount(process.env.GL_PK1);
const c0 = createClient({ chain: testnetBradbury, endpoint, account: a0 });
const c1 = createClient({ chain: testnetBradbury, endpoint, account: a1 });

const fees = async (c, payee) => {
  const messageAllocations = payee && [{
    messageType: MessageType.External, onAcceptance: false, recipient: payee, budget: PAYOUT_GAS * PAYOUT_GAS_PRICE,
    callKey: "0x" + "00".repeat(32), feeParams: encodeExternalMessageFeeParams({ gasLimit: PAYOUT_GAS, maxGasPrice: PAYOUT_GAS_PRICE }),
  }];
  const e = await c.estimateTransactionFees(payee ? { messageAllocations } : {});
  return { distribution: e.distribution, feeValue: e.feeValue, ...(payee && { messageAllocations: e.messageAllocations }) };
};
// The public RPC drops connections now and then; retry the wait, never the write.
async function wait(c, hash, waitUntil = "decided") {
  for (let i = 0; ; i++) {
    try {
      return await c.waitForTransactionReceipt({ hash, waitUntil, interval: 10000, retries: 360 });
    } catch (e) {
      if (i >= 5 || !/fetch failed|ECONNRESET|timed out|socket/i.test(String(e?.details ?? e?.message))) throw e;
      await new Promise((r) => setTimeout(r, 10000));
    }
  }
}
const check = (ok, what) => {
  console.log(ok ? "  ok  " : "  FAIL", what);
  if (!ok) process.exit(1);
};
// A leader timeout executed nothing, so sending the call again is safe. Bradbury reports it as
// NOT_VOTED, or as status LEADER_TIMEOUT with a stale FINISHED_WITH_RETURN result. It still moves
// such a transaction's value into the contract, where get_accounting shows it as unaccounted.
let stray = 0n;
async function tx(c, label, functionName, args, { value = 0n, payee } = {}) {
  for (let i = 0; ; i++) {
    const hash = await c.writeContract({ address, functionName, args, value, fees: await fees(c, payee) });
    const r = await wait(c, hash);
    const timedOut = r.statusName === "LEADER_TIMEOUT" || r.txExecutionResultName === "NOT_VOTED";
    console.log(`${label}: ${hash} ${timedOut ? "LEADER_TIMEOUT" : r.txExecutionResultName}`);
    if (!timedOut) return { ...r, hash };
    stray += value;
    if (i >= 2) return { ...r, hash, txExecutionResultName: "NOT_VOTED" };
  }
}
const read = async (functionName, args = []) => {
  const v = await c0.readContract({ address, functionName, args });
  const plain = (x) => (x instanceof Map ? Object.fromEntries([...x].map(([k, y]) => [k, plain(y)])) : Array.isArray(x) ? x.map(plain) : x);
  return plain(v);
};
const rpc = async (method, params) => {
  const r = await fetch(endpoint, { method: "POST", headers: { "content-type": "application/json", "user-agent": "slabond-e2e" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
  return (await r.json()).result;
};
// The consensus-layer transaction that finalized `hash`: it emits TransactionFinalized(txId).
async function finalizeTx(hash, fromBlock) {
  for (;;) {
    const to = BigInt(await rpc("eth_blockNumber", []));
    for (let b = fromBlock; b <= to; b += 1000n) {
      const end = b + 999n > to ? to : b + 999n;
      const logs = await rpc("eth_getLogs", [{ address: CONSENSUS_MAIN, topics: [T_FINALIZED, hash], fromBlock: "0x" + b.toString(16), toBlock: "0x" + end.toString(16) }]);
      if (logs?.length) return logs[0].transactionHash;
    }
    await new Promise((r) => setTimeout(r, 30000));
  }
}
// Settle payout `id`, paid by `paid` (a tx() result), once its transaction finalizes.
async function settle(id, paid, fromBlock) {
  await wait(c0, paid.hash, "finalized");
  const fin = await finalizeTx(paid.hash, fromBlock);
  console.log(`finalized by ${fin}`);
  const r = await tx(c1, `settle payout ${id}`, "settle_payout", [id, paid.hash, fin]);
  check(r.txExecutionResultName === "FINISHED_WITH_RETURN", "settle succeeds");
  check((await eventually(() => read("get_payout", [id]), (p) => p.status !== "SENT")).status === "DELIVERED", `payout ${id} DELIVERED`);
  r.fin = fin;
  return r;
}
const unaccounted = async () => BigInt((await read("get_accounting")).unaccounted);
// The RPC can serve state a little behind the receipt: re-read until `ok` holds (up to ~5 min).
async function eventually(fn, ok) {
  let v = await fn();
  for (let i = 0; !ok(v) && i < 30; i++) {
    await new Promise((r) => setTimeout(r, 10000));
    v = await fn();
  }
  return v;
}

let address = process.argv[3];
if (process.argv[2] === "settle") {
  // Resume: settle payout <id> paid by <tx> (e.g. after the RPC dropped mid-run).
  const [id, hash] = [Number(process.argv[4]), process.argv[5]];
  stray = await unaccounted();
  const from = BigInt(await rpc("eth_blockNumber", [])) - 50000n;
  await settle(id, { hash }, from > 0n ? from : 0n);
  const r = await tx(c1, "settle again", "settle_payout", [id, hash, (await finalizeTx(hash, from > 0n ? from : 0n))]);
  check(r.txExecutionResultName === "FINISHED_WITH_ERROR", "a payout settles once");
  check((await unaccounted()) === stray, `balance matches what the contract expects to hold (+${stray} from unexecuted txs)`);
} else if (process.argv[2] !== "claims") {
  if (!address) {
    const hash = await c0.deployContract({ code: readFileSync(process.argv[2], "utf8"), args: [], fees: await fees(c0) });
    const r = await wait(c0, hash);
    address = r.txDataDecoded?.contractAddress;
    console.log(`deploy: ${hash} ${r.txExecutionResultName} -> ${address}`);
    check(r.txExecutionResultName === "FINISHED_WITH_RETURN", "deployed");
  }
  const sid = Number(await read("get_service_count"));
  const hosts = JSON.stringify(["slabond.cristhedev.com"]);
  let r = await tx(c0, "create_service", "create_service", ["Demo API", SLA, hosts, PRICE, 30, PRICE / 1000n, 10, PRICE / 2n], { value: PRICE });
  check(r.txExecutionResultName === "FINISHED_WITH_RETURN", `service ${sid} opened`);
  r = await tx(c1, "close_service by other", "close_service", [sid]);
  check(r.txExecutionResultName === "FINISHED_WITH_ERROR", "only the provider can close");
  r = await tx(c0, "subscribe by provider", "subscribe", [sid], { value: PRICE, payee: a0.address });
  check(r.txExecutionResultName === "FINISHED_WITH_ERROR", "the provider cannot subscribe");

  const fromBlock = BigInt(await rpc("eth_blockNumber", []));
  const pid = Number((await read("get_accounting")).payout_count);
  const paid = await tx(c1, "subscribe", "subscribe", [sid], { value: PRICE, payee: a0.address });
  check(paid.txExecutionResultName === "FINISHED_WITH_RETURN", "customer subscribes");
  const sub = await read("get_subscription", [sid, a1.address]);
  check(Number(sub.start) % DAY === 0 && Number(sub.start) > Date.now() / 1000, `coverage starts ${new Date(Number(sub.start) * 1000).toISOString()}`);
  const today = Math.floor(Date.now() / 1000 / DAY) * DAY;
  r = await tx(c1, "claim today", "file_claim", [sid, today, DEMO + "status.html"]);
  check(r.txExecutionResultName === "FINISHED_WITH_ERROR", "a day that has not ended cannot be claimed");

  const p = await read("get_payout", [pid]);
  check(p.kind === "price" && p.status === "SENT" && p.to.toLowerCase() === a0.address.toLowerCase(), `payout ${pid}: price to provider, SENT`);
  const s = await settle(pid, paid, fromBlock);
  r = await tx(c1, "settle again", "settle_payout", [pid, paid.hash, s.fin]);
  check(r.txExecutionResultName === "FINISHED_WITH_ERROR", "a payout settles once");
  check((await unaccounted()) === stray, `balance matches what the contract expects to hold (+${stray} from unexecuted txs)`);
  console.log("contract", address, "service", sid);
} else {
  const sid = Number(process.argv[4] ?? 0);
  stray = await unaccounted(); // left by earlier unexecuted txs
  const svc = await read("get_service", [sid]);
  const start = Number((await read("get_subscription", [sid, a1.address])).start);
  const [outage, maintenance] = [start, start + DAY];
  check(Date.now() / 1000 > maintenance + DAY, "two covered days have ended");
  const claims = async () => await read("get_claims", [sid]);
  const claim = async (label, day, url) => {
    const n = (await claims()).length;
    const r = await tx(c1, label, "file_claim", [sid, day, url]);
    check(r.txExecutionResultName === "FINISHED_WITH_RETURN", `${label} recorded`);
    // The RPC can serve state a little behind the receipt; read until the new claim shows.
    let all = await claims();
    for (let i = 0; all.length === n && i < 30; i++) {
      await new Promise((ok) => setTimeout(ok, 10000));
      all = await claims();
    }
    const k = all.at(-1);
    check(all.length === n + 1, `${label}: claim ${k?.id} readable`);
    console.log(`    claim ${k.id} ${k.date}: ${k.decision} ${k.outage_minutes} min ${JSON.stringify(k.reason)} ${JSON.stringify(k.summary)}`);
    return k;
  };

  let r = await tx(c1, "claim off-host", "file_claim", [sid, outage, "https://example.com/status"]);
  check(r.txExecutionResultName === "FINISHED_WITH_ERROR", "a page off the provider's hosts reverts");
  r = await tx(c0, "claim by provider", "file_claim", [sid, outage, DEMO + "status.html"]);
  check(r.txExecutionResultName === "FINISHED_WITH_ERROR", "the provider is not covered");

  const before = await claims();
  let k = before.find((x) => Number(x.day) === maintenance && x.url === DEMO + "status.html");
  if (k) console.log(`    maintenance day already filed: claim ${k.id} ${k.decision} ${JSON.stringify(k.reason)}`);
  else k = await claim("maintenance day", maintenance, DEMO + "status.html");
  check(k.decision === "rejected" && BigInt(k.credit) === 0n, "announced maintenance is excluded");
  k = await claim("injected page", maintenance, DEMO + "status-injected.html");
  check(k.decision === "rejected" && Number(k.outage_minutes) === 0, "a prompt injection on the page does not create an outage");
  k = await claim("outage day", outage, DEMO + "status.html");
  check(k.decision === "approved" && Math.abs(Number(k.outage_minutes) - 45) <= 5, "45-minute outage approved");
  const credit = BigInt(k.credit);
  const cap = BigInt(svc.max_credit), perMinute = BigInt(svc.credit_per_minute);
  check(credit === (BigInt(k.outage_minutes) * perMinute < cap ? BigInt(k.outage_minutes) * perMinute : cap), `credit ${credit} = minutes x rate, capped`);
  check(BigInt((await eventually(() => read("get_service", [sid]), (x) => BigInt(x.bond) !== BigInt(svc.bond))).bond) === BigInt(svc.bond) - credit, "bond reduced by the credit");
  r = await tx(c1, "claim same day", "file_claim", [sid, outage, DEMO + "status.html"]);
  check(r.txExecutionResultName === "FINISHED_WITH_ERROR", "a day is approved once");

  r = await tx(c0, "withdraw by provider", "withdraw_credit", [k.id], { payee: a0.address });
  check(r.txExecutionResultName === "FINISHED_WITH_ERROR", "only covered customers withdraw");
  const fromBlock = BigInt(await rpc("eth_blockNumber", []));
  const pid = Number((await read("get_accounting")).payout_count);
  const paid = await tx(c1, "withdraw_credit", "withdraw_credit", [k.id], { payee: a1.address });
  check(paid.txExecutionResultName === "FINISHED_WITH_RETURN", "customer withdraws the credit");
  check((await eventually(() => read("credit_status", [k.id, a1.address]), (v) => v === "withdrawn")) === "withdrawn", "credit_status withdrawn");
  r = await tx(c1, "withdraw again", "withdraw_credit", [k.id], { payee: a1.address });
  check(r.txExecutionResultName === "FINISHED_WITH_ERROR", "a credit is withdrawn once");
  const p = await read("get_payout", [pid]);
  check(p.kind === "credit" && BigInt(p.amount) === credit, `payout ${pid}: credit ${credit}`);
  await settle(pid, paid, fromBlock);
  check((await unaccounted()) === stray, `balance matches what the contract expects to hold (+${stray} from unexecuted txs)`);
}
