# Dawnstrike activation — what Matt must do

You only need to supply **four things**. Terra should do everything else.

## 1. Give Terra your accountable email

Send Terra the email address Dawnstrike may put in its market-data user agent. This is identification for source providers, not a login.

## 2. Add your data and Vercel keys locally

Open this private file on your PC:

`C:\r\dawnstrike-state\secrets\runtime.env`

Keep the existing Telegram lines. Add these lines with your real values:

```text
ALPACA_API_KEY_ID=your_key
ALPACA_API_SECRET_KEY=your_secret
ALPACA_DATA_FEED=iex
VERCEL_TOKEN=your_vercel_token
```

Use `sip` instead of `iex` only if your Alpaca account is actually entitled to SIP. If you have an approved point-in-time universe provider, add its supported key too—for example `POLYGON_API_KEY=...`.

Do **not** paste secrets into chat, Git, screenshots, or the public config.

## 3. Tell Terra which real universe source to use

Say one of:

- “Use my approved Polygon/source key to build the dated small-cap universe,” or
- “I do not have a universe source; finish the adapter and tell me the exact provider/key I must obtain.”

Do not hand-create a ticker list. Terra must fetch, hash, validate, and register a dated source-backed list.

## 4. Enter your Windows password when prompted

When Terra reaches task registration, it should run:

```powershell
$credential = Get-Credential
```

Enter the Windows account and password that can run Dawnstrike while you are logged off and can access the network, runtime, private state, Telegram, and Vercel. Do not send the password to Terra; type it only into the Windows credential dialog.

## Then Terra does the rest

Terra must:

1. Merge the verified branch and install the exact merged SHA into
   `C:\r\dawnstrike-runtime` using the fail-closed two-phase procedure in
   [`runtime_activation_and_rollback.md`](runtime_activation_and_rollback.md).
2. Build the real source config using your email, Alpaca as primary market data, and Yahoo as independent reconciliation data.
3. Fetch and register the dated universe.
4. Register the morning, monitor, EOD, weekly-training, and finalize tasks with your credential.
5. Back up and disable the five conflicting legacy tasks—never delete them.
6. Run copy-on-write doctors and a rehearsal.
7. Deploy a Vercel preview, verify it, promote the exact verified build, and prove the public URL.
8. Watch one full unattended market day and confirm the final Telegram receipt.

## What “100% functioning” means

- The server can be operational after one clean unattended market day with readiness 200, fresh Calendar/V6 data, matching source/build/data hashes, and a verified Telegram receipt.
- The strategy cannot be called better or profitable yet. That requires at least **60 real market sessions and 100 valid sourced closed paper labels** plus every return, benchmark, risk, slippage, no-lookahead, holdout, and human-approval gate.
- Until then, the honest status is `WAITING_FOR_FORWARD_EVIDENCE`.
