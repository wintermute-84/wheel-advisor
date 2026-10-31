---
name: wheel-stage-order
description: Stage a cash-secured put or covered call as a dry run with a full risk block, transmitting to IB only when the user re-runs with --execute. Use when the user wants to sell a put, write a covered call, or place a wheel order. Requires TWS or IB Gateway running locally.
dependencies: ["trading-skills"]
---

# Wheel Stage Order

Build a single-leg short option order, price it against the live market, run the
guardrails, and print a confirmation block. **Dry run by default.**

## The rule

**You never pass `--execute`.** Run the dry run, show the user the risk block, and stop.
The user re-runs the command with `--execute` themselves. No exceptions, no "I'll go
ahead and place it", no chaining stage and execute in one step.

## IB connection

TWS or IB Gateway must be running. `IB_PORT` in `.env` sets the port (TWS: 7497 paper,
7496 live; Gateway: 4002 paper, 4001 live); `--port` overrides it. Confirm which account
a port serves with `ib-account` before trusting it. The dry run connects
read-only; `--execute` connects read-write.

## Instructions

```bash
uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py SYMBOL \
    --right P|C --strike N --expiry YYYYMMDD --limit N \
    [--qty 1] [--account ID] [--port N]
```

- `--right P` — cash-secured put. Collateral is checked against excess liquidity.
- `--right C` — covered call. **Share count is checked.** Fractional shares cannot be
  delivered on assignment; the script blocks a call written against fewer than 100 whole
  shares.
- `--limit` is the credit per share. Price it from the live bid/ask the script prints,
  not from a screener.

Exit codes: `0` staged clean, `1` blocked by a guardrail, `2` contract would not qualify.

## Rolling an existing short

```bash
uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py SYMBOL \
    --right P --strike NEW_STRIKE --expiry NEW_YYYYMMDD --limit NET_CREDIT \
    --roll-from OLD_STRIKE:OLD_YYYYMMDD [--port N]
```

Builds **one BAG combo**: BUY-to-close the old short + SELL-to-open the new leg, filled
atomically so the underlying cannot move between the two. `--limit` is the **net credit**
across both legs, as a positive number.

The new leg runs every normal guardrail. Roll mode adds:

| Check | |
|---|---|
| **Old leg must be held short** | block — otherwise the BUY leg *opens* a long put and the combo silently becomes a different trade |
| Net credit > 0 | block — paying to roll is a separate decision |
| New expiry later than old | block — a roll goes out |
| Both legs quoted live | block — IB's closed-market `-1` sentinel is rejected |
| Limit vs achievable fill | warn if above the best fill or below the worst |

The risk block prints the combo structure and the **sign** explicitly. IBKR prices combos as
BUY legs minus SELL legs, so a roll is submitted as `BUY combo @ negative limit`, which means
you *receive*. **Verify the TWS ticket shows a net credit before confirming.**

Log a roll as two journal entries — `close` then `open`.

## After a fill

Log it:

```bash
uv run python .claude/skills/wheel-journal/scripts/journal.py log SYMBOL open \
    --right P --strike N --expiry YYYYMMDD --price N --commission N
```

## Self-check

```bash
uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py --self-check
```
