---
name: wheel-journal
description: Log wheel actions (open, close, expired, assigned, called away) to an append-only ledger and report realized P&L and net cost basis per underlying. Use when the user asks how the wheel is performing, for trade history, cost basis, or after any fill.
dependencies: ["trading-skills"]
---

# Wheel Journal

Append-only ledger at `journal/trades.jsonl` plus a per-underlying rollup. This is what
makes "is the wheel actually working" answerable with realized cash instead of vibes.

## Log an action

```bash
uv run python .claude/skills/wheel-journal/scripts/journal.py log SYMBOL ACTION \
    [--right P|C] [--strike N] [--expiry YYYYMMDD] [--price N] \
    [--qty 1] [--commission 1.00] [--note "..."]
```

`ACTION` is one of `open`, `close`, `expired`, `assigned`, `called_away`.

A **roll is two entries** — a `close` then an `open`. There is no roll action.

Log every fill as it happens. A ledger reconstructed from memory weeks later is worthless.

## Report

```bash
uv run python .claude/skills/wheel-journal/scripts/journal.py report [--symbol SYM] [--json]
```

Columns: realized cash (net of commissions), gross premium collected, shares held from
assignment, open short contracts, and net cost basis per share while shares are held.

**REALIZED is cash actually moved.** Shares sitting in the account from an assignment
carry unrealized P&L the table does not show — say so when reporting, and pull the mark
from `ib-portfolio` if the user wants the full picture.

## What happens at expiry (Friday)

- **Put expires out of the money** (stock above the strike): nothing to do; it disappears
  over the weekend. Log `expired`.
- **Put finishes in the money:** it is assigned after the close. On Monday the shares are
  in the account and strike × 100 has left your cash. Log `assigned`, then the agent
  screens **covered calls** for that name, not new puts.
- **Covered call finishes in the money:** your shares are sold at the strike. Log
  `called_away`; you're back to cash and can sell a put again.
- **Pin risk:** if the stock is within about 1% of your strike at 3:30 pm ET on expiry
  day, close the option rather than leave it to chance. After-hours moves can still
  assign you.
- The GTC 50% close may already have closed the trade earlier. Log that fill as a `close`.

## Cash convention

Money in positive, money out negative, commissions always subtract. `expired` moves no
cash beyond commission — the credit was already booked at `open`.

## Self-check

```bash
uv run python .claude/skills/wheel-journal/scripts/journal.py self-check
```
