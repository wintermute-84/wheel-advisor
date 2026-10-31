# wheel-advisor

A Claude Code workspace for running the options **wheel** on an Interactive Brokers account:
sell a cash-secured put, take assignment if tested, write covered calls on the shares, repeat.

It screens candidates, sizes them against guardrails, stages orders as dry runs, and keeps a
journal of realized P&L, so you judge the strategy on cash and not on impressions.

> **Not financial advice.** This is a tool for disciplined self-directed trading. Options
> carry real risk of loss. Paper-trade it first.

## The one rule

**Nothing transmits an order unless a human types `--execute`.** The agent screens, sizes and
stages. It prints a risk block. Then it stops, and you place the trade.

## Who it's for

The defaults assume a **$20–50k account, cash-secured, ~30 DTE puts at 0.20–0.30 delta,
1 contract per position**. Everything is a percentage of your live account, so it scales.
Your own numbers come from a short conversation with the agent (below).

## Setup

You need [uv](https://docs.astral.sh/uv/), [Claude Code](https://claude.com/claude-code), TWS or
IB Gateway with the API enabled, and an **OPRA** market-data subscription (US option quotes).

```bash
git clone https://github.com/wintermute-84/wheel-advisor && cd wheel-advisor
uv sync
cp env.template .env      # set IB_PORT: TWS 7497 paper / 7496 live, Gateway 4002 / 4001
claude
```

## First session: let the agent set you up

Say **"set me up"**. The `wheel-onboard` skill interviews you about your experience, account,
emergency buffer, other holdings, permissions, tax constraints and goals. It verifies the
account live and writes `profile.toml` (gitignored). That file holds your guardrails,
exclusions and notes, and every later session reads it. Until it exists the agent answers
questions but will not screen or stage trades, and `--execute` refuses to run.

## Typical session

Just ask, e.g. *"what put should I sell this month?"* The agent runs these steps for you:

```bash
uv run python .claude/skills/ib-account/scripts/account.py --all-accounts       # live budget
uv run python .claude/skills/wheel-screen/scripts/wheel_screen.py KO PFE F \
    --excess-liquidity 30000                                                      # rank CSPs
uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py KO \
    --right P --strike 62.5 --expiry 20261120 --limit 0.70                        # dry run
# you re-run that last command with --execute, then:
uv run python .claude/skills/wheel-journal/scripts/journal.py log KO open \
    --right P --strike 62.5 --expiry 20261120 --price 0.70
uv run python .claude/skills/wheel-journal/scripts/journal.py report
```

Each wheel script has a `--self-check` (`self-check` for the journal) that tests its logic
without a network or broker connection.

## Layout

```
CLAUDE.md              advisor rules: strategy, guardrails, execution protocol
profile.example.toml   default guardrails ($20-50k, ~30 DTE)
profile.toml           yours, written by wheel-onboard (gitignored)
.claude/skills/        wheel-onboard, wheel-screen, wheel-stage-order, wheel-journal,
                       ib-account, ib-portfolio, ib-option-chain, ib-find-short-roll,
                       stock-quote, earnings-calendar, option-chain
src/trading_skills/    the engine behind the skills
journal/trades.jsonl   trade ledger (gitignored)
```

## Data sources

| Source | Used for |
|---|---|
| IB / OPRA | option quotes, IV, greeks, positions, orders. The only source trusted for pricing |
| yfinance | underlying prices, earnings dates, option **open interest** |

yfinance option quotes are broken (Yahoo returns zero bid/ask and placeholder IV), so
`src/trading_skills/options.py` nulls them. Its open interest is still genuine.

## Tests

```bash
uv run pytest -q            # some tests hit Yahoo live, with retries
uv run pytest -q -k "broker or greeks or black_scholes or utils"   # offline subset
```

## Credits

The engine and the `ib-*` / market-data skills began as
[staskh/trading_skills](https://github.com/staskh/trading_skills) (MIT). MIT licensed, see `LICENSE`.
