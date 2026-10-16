// Asserts which SLA Bond app controls appear per role and coverage (run: node app/check-actions.mjs app/index.html).
import { readFileSync } from "node:fs";
const html = readFileSync(process.argv[2], "utf8");
const js = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1];
const body = js.slice(js.indexOf("function actions"), js.indexOf("async function show"));
const esc = (s) => String(s ?? "");
const gen = (w, d = 4) => (Number(w) / 1e18).toFixed(d).replace(/\.?0+$/, "");
const make = new Function("esc", "gen", "when", "WINDOW", "EXPIRY", "ctx", `let me = ctx.me; ${body}; return actions;`);
const P = "0xp", C = "0xc", now = Date.now() / 1000, DAY = 86400;
const base = { provider: P, open: true, price: 1e15, term_days: 30, hosts: ["status.acme.example"], bond: 1e15, bond_withdrawable_after: now + 9999 };
const none = { start: 0, end: 0 }, covered = { start: now - DAY, end: now + 20 * DAY };
const run = (me, s = {}, sub = none, claims = [], avail = [], recov = 0, pending = []) =>
  make(esc, gen, () => "T", 7 * DAY, 90 * DAY, { me })({ ...base, ...s }, sub, claims, avail, recov, pending);
const check = (name, ok) => { console.log(ok ? "ok  " : "FAIL", name); if (!ok) process.exitCode = 1; };
const claim = { id: 4, date: "2030-01-05", decision: "approved", credit: 1e14, reserved: 2e14, at: now - 100 * DAY };

check("no wallet: prompt only", run(null).includes("Connect a wallet"));
check("outsider on an open service: subscribe, no claim form", run(C).includes("Subscribe for") && !run(C).includes('data-form="file_claim"'));
check("covered customer: renew and claim form", run(C, {}, covered).includes("Renew for") && run(C, {}, covered).includes('data-form="file_claim"'));
check("lapsed inside claim window: can still claim, renew blocked", /data-act="subscribe"[^>]*disabled/.test(run(C, {}, { start: now - 40 * DAY, end: now - DAY })) && run(C, {}, { start: now - 40 * DAY, end: now - DAY }).includes("file_claim"));
check("lapsed after claim window: no claim form", !run(C, {}, { start: now - 60 * DAY, end: now - 30 * DAY }).includes("file_claim"));
check("closed service: no subscribe", !run(C, { open: false }, covered).includes('data-act="subscribe"'));
check("available credit: withdraw button", run(C, {}, covered, [claim], [4]).includes('data-act="withdraw_credit" data-arg="4"') && !run(C, {}, covered, [claim], []).includes("withdraw_credit"));
check("provider: top up and close, never subscribe or claim", run(P).includes("close_service") && run(P).includes("top_up") && !run(P).includes("subscribe") && !run(P, {}, covered).includes("file_claim"));
check("provider: bond withdraw only after closing, disabled before the window", !run(P).includes("withdraw_bond") && /withdraw_bond"[^>]*disabled/.test(run(P, { open: false })) && /withdraw_bond"(?![^>]*disabled)/.test(run(P, { open: false, bond_withdrawable_after: now - 1 })));
check("provider: sweep unclaimed credits after 90 days", run(P, {}, none, [claim]).includes("sweep_claim") && !run(P, {}, none, [{ ...claim, at: now }]).includes("sweep_claim"));
check("recover only with a failed transfer", run(C, {}, none, [], [], 5).includes('data-form="recover"') && !run(C).includes('data-form="recover"'));
check("pending payout: settle button", run(C, {}, none, [], [], 0, [{ id: 3, kind: "credit", amount: 1e14 }]).includes('data-act="settle_payout" data-arg="3"') && !run(C).includes("settle_payout"));
