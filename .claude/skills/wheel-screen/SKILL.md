---
name: wheel-screen
description: Screen cash-secured put candidates the account can actually afford, applying every profile guardrail (collateral cap, credit floor, delta band, DTE band, liquidity, earnings). Use when the user asks what put to sell, what to wheel, or for CSP candidates. Requires TWS or IB Gateway running locally.
dependencies: ["trading-skills"]
---

# Wheel Screen

Rank cash-secured put candidates against the account guardrails.

## Data sources

Live quotes, IV, and greeks come from **IB (OPRA)**. Open interest is backfilled from
yfinance because IB's real-time tickers do not carry it.

**Do not screen from yfinance quotes.** Yahoo returns `bid: 0.0, ask: 0.0` and
placeholder IV (50.0 / 25.0 / 12.5) for option chains — verified broken, not a transient.

## IB connection

TWS or IB Gateway must be running. `IB_PORT` in `.env` sets the port (TWS: 7497 paper,
7496 live; Gateway: 4002 paper, 4001 live); `--port` overrides it. Confirm which account
a port serves with `ib-account` before trusting it.

## Instructions

### Step 1 — get the real collateral budget

Never guess it. Read it first:

```bash
uv run python .claude/skills/ib-account/scripts/account.py --all-accounts
```

Use `excess_liquidity` from that output.

### Step 2 — pick a candidate universe

The screener does not pick symbols; you do. Choose liquid optionable names the user
would be content to own 100 shares of. The `excluded` and `overweight` lists in
`profile.toml` are skipped automatically. Avoid names correlated with what the user
already holds; read the book with `ib-portfolio` if unsure.

### Step 3 — screen

```bash
uv run python .claude/skills/wheel-screen/scripts/wheel_screen.py SYM [SYM ...] \
    --excess-liquidity <from step 1> [--port N] [--json] [--verbose]
```

`--verbose` adds per-strike reject reasons — use it when nothing passes, to show the
user *why* rather than silently returning an empty list.

### Step 4 — report

Present the top candidates with delta, DTE, credit, collateral, breakeven, downside
cushion, and annualized yield. State the next earnings date. Then hand off to
`wheel-stage-order` for the dry run — do not stage it yourself unless asked.

## Guardrails applied

From `profile.toml` (defaults in `profile.example.toml`): collateral ≤ 35% of excess
liquidity · credit ≥ $35 · delta 0.20–0.30 · 21–45 DTE · OI ≥ 500 · bid/ask ≤ 10% of mid ·
underlying ≥ $5 · no earnings inside the window.

If nothing passes, **say so and report the reject reasons.** Do not loosen the filters
to manufacture a trade.

## Self-check

```bash
uv run python .claude/skills/wheel-screen/scripts/wheel_screen.py --self-check
```
