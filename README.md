# wheel-advisor

A [Claude Code](https://claude.com/claude-code) workspace that helps you run the options
**wheel** on an Interactive Brokers account, one carefully checked trade at a time.

You talk to it in plain English. It interviews you, sets limits that fit your account,
finds candidate trades, prepares each order with a full risk summary, and keeps a ledger
of what you actually earned. **It never sends an order itself.**

> **Not financial advice.** This is a tool for disciplined, self-directed trading. Options
> can lose real money. Start on a paper (simulated) account, and stay there until you've
> completed a few full cycles.

## Contents

- [How the wheel works](#how-the-wheel-works)
- [What you need](#what-you-need)
- [Setup](#setup)
- [Your first session](#your-first-session)
- [Day-to-day use](#day-to-day-use)
- [A full cycle, step by step](#a-full-cycle-step-by-step)
- [Safety rails](#safety-rails)
- [Going live](#going-live)
- [Troubleshooting](#troubleshooting)
- [Glossary](#glossary)
- [Running the scripts yourself](#running-the-scripts-yourself)
- [Project layout](#project-layout)

## How the wheel works

1. **Sell a cash-secured put.** Someone pays you now (the *premium*) for the right to make
   you buy 100 shares at a price you pick (the *strike*) before a date (the *expiry*). The
   cash to buy them stays set aside in your account.
2. **If the stock stays above the strike,** the option expires worthless. You keep the
   premium and go back to step 1. Often you buy it back early at half the price and keep
   50% of the premium.
3. **If it falls below,** you buy the 100 shares at the strike (*assignment*).
4. **Sell a covered call** on those shares: someone pays you for the right to buy them at a
   higher price. If they do (*called away*), you're back to cash and step 1.

The defaults suit a **$20–50k account**: puts about 35 days out, at a strike roughly 5–10%
below the current price (0.20–0.30 *delta*), one contract at a time, at most two open
positions to start.

## What you need

| | |
|---|---|
| An Interactive Brokers account | with options trading permission (the lowest level covers cash-secured puts and covered calls) and its **paper trading** account |
| TWS or IB Gateway | IB's desktop app; it must be running and logged in while you trade |
| OPRA market data | live US option quotes, a paid IB subscription (about $1.50/month for non-professionals; check IB's current price) |
| [uv](https://docs.astral.sh/uv/) | installs the Python tools (Python 3.13 is fetched automatically) |
| [Claude Code](https://claude.com/claude-code) | the agent you talk to |

## Setup

### 1. Get the code

```bash
git clone https://github.com/wintermute-84/wheel-advisor
cd wheel-advisor
uv sync
cp env.template .env          # IB_PORT=7497 is TWS paper trading
```

### 2. Turn on the TWS API

Log into **TWS with your paper trading username**, then open
*File (or Edit) → Global Configuration → API → Settings*:

- tick **Enable ActiveX and Socket Clients**
- **Socket port:** `7497` (TWS paper). Live TWS is `7496`; IB Gateway uses `4002` paper and `4001` live
- tick **Allow connections from localhost only**
- leave **Read-Only API** ticked for now. Dry runs work with it on; untick it when you're
  ready to send orders with `--execute`

The first time a script connects, TWS asks whether to accept the connection: accept it.

### 3. Market data

- Subscribe to **OPRA** (US options) in Client Portal → *Settings → Market Data Subscriptions*.
  Without it, every order is blocked with "no live bid/ask".
- **Paper accounts don't get data by default.** In Client Portal → *Settings → Account
  Settings → Paper Trading Account*, enable sharing your market data with the paper
  account. It can take up to a day to start working.
- Optional: a US equity feed (NYSE/NASDAQ) lets IB compute option greeks directly. Without
  it the tools work out delta from the live option price instead, which is expected, not
  an error.

### 4. Check the connection

```bash
uv run python .claude/skills/ib-account/scripts/account.py --all-accounts
```

You should see your paper account (its id starts with `DU`). If it says it can't connect,
see [Troubleshooting](#troubleshooting).

## Your first session

From the repo folder run `claude` and say:

> **set me up**

The agent explains the wheel in plain words, then asks about:
- your experience
- your broker connection
- your account size and currency
- the emergency cash you keep elsewhere
- your other holdings
- your market data
- your tax situation (it records this; it doesn't advise)
- your goal

It verifies your account live and writes `profile.toml` (kept private by `.gitignore`).
That file holds your limits and notes, and every later session reads it. Until it exists,
the agent answers questions but won't prepare trades.

It finishes with your numbers, for example: *"At $20k you get two positions with strikes
up to $60."* It also suggests a short list of names that fit, starting from a
beginner-friendly core: SPYM (S&P 500), XLF, BAC, KO, XLP and others.

## Day-to-day use

Just talk to it. Useful things to say:

| Say | What happens |
|---|---|
| "what's my status?" | reads your account and positions, flags anything that needs managing |
| "what put should I sell?" | screens your watchlist and ranks the safest candidates |
| "stage the KO 85 put for Dec 18" | builds the order as a **dry run** with a full risk summary |
| "it filled at 0.90" | logs the trade and prepares the 50% take-profit order |
| "my BAC put is at 21 days, what now?" | applies the management rules: hold, roll once, or close |
| "I got assigned on XLF" | logs it and looks for a covered call |
| "how am I doing?" | realized profit and cost basis from the ledger |

Every order comes back as a block like this, with nothing sent:

```
  DRY RUN — CASH-SECURED PUT   (nothing has been transmitted)
  SELL 1 KO 20261218 85P @ LMT 0.90

  In plain words: if KO closes below $85 on 2026-12-18, you buy 100 shares
  for $8,500. That cash stays locked until then. The most you can lose
  is those shares going to zero, minus the $90 you collect now.

  delta 0.24   implied vol 21.0%   IV rank 44   premium: NORMAL   DTE 48 ...
  credit $90.00   collateral $8,500.00   breakeven $84.10   ...
```

To send it, **you** re-run the same command with `--execute` (the agent shows you the
exact command). The agent never does this step.

## A full cycle, step by step

1. **Session start.** The agent checks your account (PAPER or LIVE), open positions and
   free cash.
2. **Screen.** Candidates from your watchlist, safest cushion first. Names with earnings
   before expiry, thin trading or too little premium are filtered out.
3. **Dry run.** Read the risk block. If the premium is labelled THIN, the agent suggests
   waiting.
4. **Execute.** You re-run with `--execute`, then confirm the fill in TWS.
5. **Log + take profit.** Tell the agent the fill price. It logs it and stages a resting
   buy-to-close at half the credit, which you execute too. That closes the trade at 50%
   profit without you watching.
6. **21 days to expiry.** If the trade is still open, the agent applies the rules: hold, roll
   once for a credit, or close. Never more than 2 rolls or 90 days per position.
7. **Expiry Friday.**
   - Out of the money: it expires and you log `expired`.
   - In the money: you're assigned. Shares appear Monday and the cash is spent; log
     `assigned`.
   - If the price sits within about 1% of the strike late on expiry day, close it rather
     than gamble.
8. **Covered call.** With 100 shares, the agent finds a call at 0.15–0.20 delta, at or
   above your cost basis while you're a beginner. When it's called away, log
   `called_away` and start again at step 2.

## Safety rails

Enforced by the code, not just by instructions:

- **Nothing is sent without `--execute`,** and the agent never types it.
- **Paper first:** `--execute` only works on a paper account until you deliberately switch
  to live (see below).
- **100% cash-secured by default:** every put's full cost must fit in your *free* settled
  cash, after the cash already promised to other open puts. Assignment never needs a
  loan. Onboarding explains the trade-off if you ask about using margin instead, and
  beginners can't switch it off.
- **Size limits:** 30% of excess liquidity per position, 50% across all open puts, 2
  positions, 1 contract. These grow only after 3 profitable cycles and a $50k account.
- **No new put while another is in the money.**
- **Contract quality:** no earnings before expiry; enough open interest to get out; a
  bid/ask spread under 10%; at least $35 of premium; no stocks under $5.
- **Covered calls** need 100 whole shares.
- **A bad `profile.toml` won't load,** rather than silently loosening a limit.

## Going live

When you've completed a few profitable cycles on paper, say **"I want to go live"**. The
agent reviews your paper record, checks the live connection, and sets
`live_trading = true` in your profile. Your size limits stay at the beginner level until
you've earned the next step. Remember to untick *Read-Only API* in the live TWS too.

## Troubleshooting

| You see | Cause and fix |
|---|---|
| `Could not connect to IB on port 7497` | TWS/Gateway isn't running or logged in, the API isn't enabled, or the port differs. Check Setup step 2 and `IB_PORT` in `.env` |
| `no live bid/ask` | no OPRA subscription, the paper account isn't sharing market data yet, or the market is closed. Quotes can take a few seconds; retry once |
| `note: IB has no live market data for some contracts` | same as above. It's shown once per run instead of an error per contract |
| `--execute` blocked: not a paper account | working as intended. See [Going live](#going-live) |
| an order doesn't transmit | *Read-Only API* is still ticked in TWS |
| `earnings date could not be loaded` | Yahoo didn't return a date for that stock; check it yourself before trading |
| something looks wrong | re-run the command with `WHEEL_DEBUG=1` in front to see all library and IB messages |

## Glossary

| Term | Meaning |
|---|---|
| **Cash-secured put (CSP)** | selling a put while holding enough cash to buy the shares if assigned |
| **Assignment** | the put buyer makes you buy the shares at the strike |
| **Covered call** | selling a call on 100 shares you own |
| **Called away** | your shares are sold at the call's strike |
| **Strike** | the price per share the option is about |
| **Premium / credit** | what you're paid for selling the option (price × 100) |
| **Breakeven** | strike minus premium: below this, the trade loses money |
| **DTE** | days to expiry |
| **Delta** | roughly the chance the option ends in the money; 0.25 ≈ 25% |
| **Open interest (OI)** | how many contracts are open; more means easier to get out |
| **Bid/ask spread** | the gap between buy and sell prices; wide spreads cost you money |
| **Excess liquidity** | IB's measure of spare capacity in your account |
| **Settled cash** | cash that has finished settling and can be spent |
| **Roll** | buying back an option and selling a later one in a single order |
| **IV / IV rank** | how expensive options are now, and versus the past year |
| **Cycle** | from opening a position until you're back to cash |

## Running the scripts yourself

The agent runs these for you; you only need them to `--execute`, or if you prefer the
terminal.

```bash
# account and positions
uv run python .claude/skills/ib-account/scripts/account.py --all-accounts
uv run python .claude/skills/ib-portfolio/scripts/portfolio.py

# rank put candidates against your limits
uv run python .claude/skills/wheel-screen/scripts/wheel_screen.py SPYM XLF BAC \
    --excess-liquidity 30000

# dry run a put, then send it yourself by adding --execute
uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py KO \
    --right P --strike 85 --expiry 20261218 --limit 0.90

# the 50% take-profit (resting buy-to-close at half the credit)
uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py KO \
    --right P --strike 85 --expiry 20261218 --limit 0.45 --close

# roll out to a later expiry for a net credit
uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py KO \
    --right P --strike 85 --expiry 20270115 --limit 0.40 --roll-from 85:20261218

# ledger
uv run python .claude/skills/wheel-journal/scripts/journal.py log KO open \
    --right P --strike 85 --expiry 20261218 --price 0.90
uv run python .claude/skills/wheel-journal/scripts/journal.py report
```

Each wheel script has a `--self-check` (`self-check` for the journal) that tests its rules
without a network or broker. Exit codes: `0` ok, `1` blocked by a rule, `2` can't
connect or the contract wasn't found.

## Project layout

```
CLAUDE.md              the agent's rules: strategy, limits, how it talks to you
profile.example.toml   default limits (beginner, $20-50k, ~35 DTE)
profile.toml           yours, written by onboarding (private, gitignored)
.claude/skills/        wheel-onboard, wheel-screen, wheel-stage-order, wheel-journal,
                       ib-account, ib-portfolio, ib-option-chain, ib-find-short-roll,
                       stock-quote, earnings-calendar, option-chain
src/trading_skills/    the engine behind the skills
journal/trades.jsonl   your trade ledger (private, gitignored)
```

Prices come from IB (option quotes, always live) and Yahoo Finance (stock prices, earnings
dates and open interest; Yahoo's option quotes are broken and ignored).

## Tests

```bash
uv run pytest -q                                                    # some tests call Yahoo
uv run pytest -q -k "broker or greeks or black_scholes or utils"   # offline subset
```

## Credits

The engine and the `ib-*` / market-data skills began as
[staskh/trading_skills](https://github.com/staskh/trading_skills) (MIT). MIT licensed; see `LICENSE`.
