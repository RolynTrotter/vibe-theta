---
name: vibe-theta
description: Runs the far-OTM SPX theta-harvest experiment (ERN-style 0DTE and overnight short puts, AAPL decumulation into SPY, T-bill sleeve) for two paper portfolios, one gated by a System One gut check. Use for the scheduled morning, afternoon and close runs, to test the approval pipeline, or when asked how the theta experiment is doing.
---

# vibe-theta

Paper-trades Early Retirement Now's short-dated SPX put selling in two portfolios that
differ only in the gut check:

- **A, Gut-checked**: trades only when the Jev classifier says the day feels ordinary.
- **B, Always**: trades whenever the hard rules pass.

Both start with 900 AAPL (stepped-up basis = starting price unless `config.local.json`
says otherwise) plus a $50k T-bill sleeve (SGOV), about $350k. Both sell AAPL on the
same decumulation rules and sweep cash to the sleeve, then SPY.

The engine (`scripts/theta.py`) only does arithmetic on JSON files. You do the data
gathering (IBKR connector), the gut check (vibe-classification), the dashboard writes
(ArtifactData) and the notifications.

**Never place or submit a live order.** The IBKR connector can only create order
*instructions* that a person submits in the IBKR app; that tap is the approval.

## Setup each run

1. Code: work in a checkout of `RolynTrotter/vibe-theta` (attach with add_repo if needed).
   `cd` into it. `config.local.json` holds `dashboard_url` and any overrides.
2. State: `ArtifactData get` collection `state`, doc `current` from the dashboard, with
   `out_dir: db`. Copy `db/state/current.json` to `state.json` and remember its `version`
   (pass it as `--state-version` to every command that changes state).
   If the doc does not exist, run `init` (below) first.
3. Writes: every command adds files to `outbox/` and lists them in
   `outbox/manifest.json`. At the end of the run, send all of them in one
   `ArtifactData batch` (`writes` = the manifest entries; each has `op`, `collection`,
   `doc_id`, `file_path` and, for the state doc, `if_version`). If the state write
   conflicts, re-read state and redo the run's commands; never force.
   Then `rm -rf outbox`.

IBKR contract ids: SPX index 416904 (option chains), SPY 756733, AAPL 265598, SGOV 424099317.
SPX index quotes are not subscribed; SPX = SPY × the stored ratio (recalibrated at close).
Option quotes are ~15 min delayed; paper fills are at the delayed bid.

## Before anything else

If today is not a US market trading day (weekend, NYSE holiday, or no SPXW expiration
dated today in `get_option_parameters`), stop quietly: no writes, no notification.

## Quotes snapshot (all runs)

`get_price_snapshot`:
- SPY with `last, bid_ask, prior_close, open, change, implied_vol_underlying, historical_vol, implied_volatility_percentile`
- AAPL with `last, prior_close`; SGOV with `last`

IBKR often returns `prior_close` empty: use `last − change.change` (valid during
regular hours; after the close `change` is measured from today's close).
Map to `spy: {last, prior_close, open, iv (annual_iv), hv30, iv_pctile_52w (52-week value)}`,
`aapl: {last, prior_close}`, `sgov: {last}`.

## Morning run, ~9:46 ET: 0DTE put

1. Quotes snapshot. SPX ≈ SPY last × `spx_spy_ratio` from state.
2. `get_option_parameters(416904)`; take the expiration with today's date and trading
   class `SPXW`. `get_option_data` with `min_strike` = SPX × 0.90, `max_strike` = SPX × 0.985.
3. Take every strike divisible by 25 and `get_price_snapshot` its **put** with
   `bid_ask, option_midpoint_iv`. Keep rows `{strike, bid, ask, iv: annualIv (skip if
   isValid is false), contract_id_ex: put_contract_id_ex}`. Note `top_status` once.
4. Write `snapshot.json`:
   `{asof (now, ET ISO), leg: "0dte_put", expiry: today, spy, aapl, sgov, chain, quote_status}`.
5. `python3 scripts/theta.py plan --snapshot snapshot.json --state-version V`
   - If it prints `refine` strikes, snapshot the puts for those that exist in the
     `get_option_data` result, append them to the chain and re-run `plan`.
   - If `rules_failed` says to extend the chain, fetch lower strikes and re-run.
6. Gut check inputs: WebSearch for today's US economic calendar (Fed decision, CPI,
   PPI, jobs report, major earnings that move the index) and for market news this
   morning. Write one sentence of scheduled events and 2 to 4 short neutral headlines
   in your own words. Do not tell Jev what the rules or prices decided.
7. `python3 scripts/theta.py jev-input --plan plan.json --events "..." --headlines "..."`
   then `python3 /mnt/skills/plugins/vibe-classification/scripts/jev.py < jev_in.json > jev.json`.
   If Jev fails, continue without `--jev` (A is then treated as a reject and the
   notification says why).
8. `python3 scripts/theta.py gate --plan plan.json --jev jev.json --events "..." --headlines "..."`
9. `python3 scripts/theta.py fill --plan plan.json --state-version V` (paper fills, both portfolios).
10. Approval pipeline (below), then send the outbox, then notify.

## Afternoon run, ~3:01 PM ET: overnight put + AAPL decumulation

Same as the morning run with `leg: "1dte_put"`, expiry = the next SPXW expiration after
today, `min_strike` = SPX × 0.85, `max_strike` = SPX × 0.97, sampling every 50 points
instead of 25 (refine fills in around the pick). Refresh the gut check with
afternoon headlines. After `fill`:

`python3 scripts/theta.py decum --snapshot snapshot.json --state-version V`

Decumulation is identical in both portfolios and is not gated: a tranche on AAPL
down-days or below basis, a bigger tranche on crash days or after a stop-out, and a
pace backstop. Mention any sale in the notification.

## Close run, ~4:20 PM ET: stops, settlement, sweep, mark

1. Quotes snapshot. `quotes.json` = `{spy, aapl, sgov, spy_close}` (spy_close = SPY last).
2. Bars: `get_price_history` SPY, `security_type STK`, `step FIVE_MINS`,
   `outside_rth false`, `period ONE_DAY` (use `TWO_DAYS` if state has an open overnight
   put from the previous session; `THREE_DAYS`/`ONE_WEEK` after weekends or holidays).
   Save the raw result as `bars.json`.
3. Ratio: on the next SPXW expiration, snapshot the call and put at the strike nearest
   SPX (bid_ask). ratio = (call mid − put mid + strike) / SPY close. Use it only if
   between 9.9 and 10.2.
4. `python3 scripts/theta.py settle --quotes quotes.json --bars bars.json --ratio R --state-version V`
5. Approvals: `ArtifactData query` collection `approvals` for today's plan ids; the
   dashboard writes `{decision, at}` there. Copy the decision into the plan doc's
   `approval.status` (in the outbox: edit `plans/<id>` or re-run `link`).
   `get_order_instructions` and `delete_order_instruction` every test instruction this
   skill staged (a 1-lot BUY at $0.05 on an SPXW put), from today or earlier. Never
   delete anything else. Report what was deleted.
6. Send the outbox. Notify only if something happened: a stop, an in-the-money
   settlement, a forced sale, or a decumulation sale.

## Approval pipeline

The click-through exists only when the plan passes: hard rules passed and, for the
gut-checked portfolio, Jev passed. A rejected day still gets a notification, but with
no links, so acting on it takes deliberate effort.

When the plan passes:
1. Staging (`config.approvals.stage_ibkr`):
   - `test`: `python3 scripts/theta.py stage --plan plan.json --mode test` and call
     `create_order_instruction` with its `instruction` block. It BUYS one of the chosen
     puts at $0.05, so even if someone submits it the worst case is owning one far-OTM
     put (about $6). It exercises the real path (contract, deep link, approval) without
     risk. Keep the URL and id from the result.
   - `live` (only when `state.mode` is `live` AND Rolyn has switched it on in chat):
     same with `--mode live --portfolio A`; the message must also give the stop-limit
     to attach (`stop_to_attach`) and suggest a `create_alert` at `alert_spx_level`.
   - `off`: no IBKR instruction.
2. `python3 scripts/theta.py link --plan plan.json --dashboard "<dashboard_url>#<plan_id>" --ibkr-url "<url>" --instruction-id "<id>" --staging test`

When it does not pass: `python3 scripts/theta.py link --plan plan.json` (records
`no-click-through`).

## Notifications

1. `PushNotification` (under 200 characters), for example:
   `Theta 0DTE: sell 7450P @0.10 (4.0% OTM), gut 0.66 OK. Approve in session.` or
   `Theta 0DTE: gut check says NO (0.18), A sits out, B sold 7450P.`
2. Then a `SendUserMessage` with the same facts plus, **only when the plan passed**, the
   dashboard link and the IBKR instruction link. On reject days include the reason and
   no links.

## Init (once)

Quotes snapshot, then `python3 scripts/theta.py init --quotes q.json` with
`q.json = {spy, aapl, sgov}`, and send the outbox.

## Reading the results

`python3 scripts/theta.py report`, or open the dashboard: equity curves for A and B,
the day's probability space (risk-neutral vs fat-tailed realized distribution, P(ITM)
and P(stop) by strike, expected value per contract), crash tables, events and approvals.

## Model limits worth stating when reporting

- Option quotes are 15-minute delayed; SPX is derived from SPY.
- Stops are simulated from 5-minute SPY bars with a crash vol bump of 2 vol points per
  1% drop. A move inside a bar fills at the limit; a bar that opens past the stop is a
  gap, and the stop-limit stays unfilled unless prices come back.
- Margin is a rough portfolio-margin approximation, not IBKR's calculation.
- Taxes are tracked as 1256 option P&L, AAPL long-term gains/losses and SPY gains; the
  dashboard's tax numbers are estimates, not advice.
