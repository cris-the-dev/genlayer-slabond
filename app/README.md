# Demo app

A static page that talks to the contract deployed on GenLayer Testnet Bradbury (address in the page header).
Reads work without a wallet; writes go through MetaMask.

Files:
- `index.html` — the page
- `gl.js` — wallet + read/write helpers
- `app.css` — shared styles
- `check-actions.mjs` — asserts which controls the page shows per role and coverage (`node app/check-actions.mjs app/index.html`, runs in CI)
- `genlayer.js` — not committed; a browser bundle of `genlayer-js@2.0.0-rc.2` (Bradbury Phase 2, fee deposits):

```bash
npm i genlayer-js@2.0.0-rc.2 esbuild
printf 'export { createClient, createAccount, MessageType, encodeExternalMessageFeeParams } from "genlayer-js";\nexport { testnetBradbury } from "genlayer-js/chains";\n' > entry.js
# rc.2's fee estimate calls FeeManager without a sender and fails on Phase 2 (insufficient funds
# for 0x0); pass the connected account until upstream fixes it:
sed -i.bak 's/publicClient.call({ to: address, data, gasPrice: receiptGasPrice/publicClient.call({ account: client.account?.address, to: address, data, gasPrice: receiptGasPrice/' node_modules/genlayer-js/dist/index.js
npx esbuild entry.js --bundle --format=esm --minify --platform=browser --outfile=app/genlayer.js
```

Paying calls (`subscribe`, `withdraw_credit`, `withdraw_bond`, `recover`) send GEN out of the contract as an
external message, whose gas comes from the calling transaction's fee deposit: `gl.js` adds an External message
allocation for the payee (100k gas at 1 gwei; the estimated deposit for such a call is about 0.1 GEN higher). A paying call sent
without that allocation still succeeds but the transfer is never executed (see the main README).

Serve the folder with any static server (`python -m http.server -d app`). The page loads Switzer from `/fonts/`; without it the system font is used.
