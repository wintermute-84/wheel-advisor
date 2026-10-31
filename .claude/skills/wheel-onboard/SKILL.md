---
name: wheel-onboard
description: First-run interview that personalizes the wheel advisor and writes profile.toml. Use when profile.toml is missing, when the user says "set me up", "onboard me", "personalize", "go live", or when their account, experience or holdings change materially.
---

# Wheel Onboard

Interview the user and write `profile.toml`, which overrides `profile.example.toml`.
Ask **one topic at a time**, conversationally. Offer the default in brackets so the user
can just say "default". This is a conversation, not a form.

If `profile.toml` already exists, read it and ask what changed instead of starting over.
If the user asks to **go live**, jump to "Switching to live" at the bottom.

## Before the first question: say this in plain words

> Here's what the wheel is. You sell a **cash-secured put**: someone pays you now for the
> right to make you buy 100 shares at a price you choose (the strike), any time before a
> date (the expiry). The cash to buy those shares stays in your account the whole time.
> If the stock stays above the strike, the option expires, and you keep the payment and
> do it again. If it falls below, you buy the shares; that's called **assignment**. Then
> you sell a **covered call**: someone pays you for the right to buy your shares at a
> higher price. If they do, you're back to cash and start over.
>
> The most you can lose on a put is the shares going to zero, minus what you were paid.
> That's real money, so we start on a **paper** (simulated) account. I never send an order
> myself: I prepare it, show you every number, and you send it by re-running a command
> with `--execute`. I can't watch the market for you. You come back and I check your
> positions each session.

Then ask if they have questions before continuing.

## The interview

1. **Experience.** Have they sold options before? Run the wheel? Set `experience` to
   `beginner`, `intermediate` or `advanced` [beginner]. If they've closed profitable wheel
   cycles before (paper counts), record how many in `profitable_cycles` [0].
2. **Broker connection.** TWS or IB Gateway, and which port? Walk them through README >
   Setup if needed. Set `IB_PORT` in `.env` [7497, TWS paper]. Verify with
   `uv run python .claude/skills/ib-account/scripts/account.py --all-accounts` and say
   whether it's PAPER (account id starts with `DU`) or LIVE. **New users start on paper.**
   Never write `live_trading = true` in a first interview.
3. **Account size, type and currency.** Cash or margin account? Base currency? Confirm
   `net_liquidation`, `excess_liquidity` and cash from step 2. If the base currency isn't
   USD, note it in `notes`: every cap must be converted. Record a rough `size_usd`. Ask
   about open short options too; they count toward the position and collateral caps.
4. **Emergency buffer.** How much cash do they keep outside the broker, and how many
   months of spending does it cover? It must stay outside IBKR [3–6 months of spending].
5. **The rest of their net worth.** Other accounts, crypto, property, a large single stock?
   Any name above ~10% of their total net worth goes in `overweight`. Ask about correlation
   too: a tech job plus tech stocks is one cluster.
6. **Permissions and market data.** Options level (cash-secured puts and covered calls
   need the lowest level). OPRA subscription? On paper, is "share market data" enabled?
   Without live option quotes nothing can be staged. See README > Market data.
7. **Strategy.** Keep the defaults unless they give a reason: ~35 DTE (band 28–45),
   0.20–0.30 delta, $35 credit floor, take profit at 50%. **Sizing starts at the beginner
   row and the first interview never raises it:** 30% of excess liquidity per position,
   50% total, 2 positions, 1 contract. Explain the trade-off once: a higher delta pays
   more and gets assigned more often.
8. **Collateral: recommend 100% (`cash_secured_frac = 1.0`).** Explain both sides, then
   let them choose. Beginners stay at 100%; the profile refuses anything lower for them.

   > **Fully cash-secured (recommended).** Every put has its full cost (strike × 100) set
   > aside in cash. If you're assigned, you simply pay for the shares. There's no loan, no
   > interest and no margin call, and a market crash can't force IB to sell you out. The
   > cost: your cash backs fewer positions, so returns on the account are lower.
   >
   > **Lower collateral (margin-secured).** You set aside only part of the cost, say 50%,
   > and borrow the rest on margin if assigned.
   > - *Benefit:* more or bigger positions from the same cash, so a higher return on
   >   capital when things go well.
   > - *Risk:* losses grow in the same proportion as the leverage. Puts get assigned
   >   together in a sell-off, which is exactly when margin requirements rise. You then
   >   pay interest on the borrowed part, and IB can sell your positions without asking,
   >   at the worst prices. A drop that would be a paper loss on a cash-secured account
   >   can become a forced, realized one. It also breaks the "I'm happy to own these
   >   shares" premise, because you can't actually afford all of them.

   Only an intermediate or advanced user who explicitly asks gets a lower value
   (0.25–1.0). Record their reasoning in `notes`. The position and total caps still count
   the full obligation either way.
9. **Universe.** Build their list from the starter menu below, filtered to what fits.
   Names they never want to sell puts on go in `excluded` (covered-call funds such as
   QYLD/JEPQ are excluded by default).
10. **Tax and constraints.** Where are they tax resident? Anything constraining them
   (withholding on US dividends, wash-sale rules, an employer trading policy)? **Record it
   in `notes`; don't give tax advice.** Suggest a professional for anything uncertain.
11. **Goal.** Income, buying shares cheaply, or learning? Learning favours staying on
    paper longer.

## Starter menu (a conversation base, not a recommendation)

**Filter by their live cap first.** Pull each name's price with `stock-quote`. A 0.20–0.30
delta strike sits roughly 5–10% below the price, so one contract ties up about
`0.93 × price × 100`. It fits only if that is at most 30% of excess liquidity: about $6k
at $20k, $10.5k at $35k, $15k at $50k. Never show a name that doesn't fit.

**Beginner core** (the only names offered until `profitable_cycles >= 3`):

| Name | What it is | Collateral (late Oct 2026) | Fits from |
|---|---|---|---|
| SPYM | S&P 500 ETF (low-priced share class; was SPLG) | ~$8.3k | ~$28k |
| XLF | US financials ETF, no earnings | ~$5.3k | ~$18k |
| BAC | Bank of America | ~$4.7k | ~$16k |
| PFE | Pfizer | ~$2.8k | $20k (credit is often under $35) |
| KO | Coca-Cola | ~$8.8k | ~$30k |
| XLP | US consumer staples ETF | ~$8.3k | ~$28k |
| CSCO | Cisco | ~$9.7k | ~$33k |
| WMT | Walmart | ~$11.5k | ~$38k |

**Intermediate** (after 3 profitable cycles, opt-in): XLE, OXY (energy) · KRE, C
(financials) · EWZ, EEM (emerging markets) · SLV, GDX (metals) · NKE, PYPL, UBER, DIS ·
TLT (bonds, only when the credit clears $35).

**High-volatility slot** (at most one at a time, never the first trade, intermediate
only): SOFI, HOOD, IBIT, INTC, XBI.

Never offered at this account size: SPY, VOO, IVV, QQQ, IWM. One contract is most or all
of the account.

Rules for building their list:

1. **First trade:** the first core name that fits and clears $35 at 0.20–0.30 delta, in
   the order SPYM, XLF, BAC, KO, PFE. If none qualifies, tell them plainly the account
   can't wheel a low-volatility name yet. The fix is patience or a larger account, **not**
   SOFI or IBIT.
2. **Be honest about the credit floor.** Under ~$25k, few calm names pay $35 at
   0.20–0.30 delta. Expect PFE, and often BAC, to fail it. Never widen the delta band to
   make a cheap stock pay.
3. **One name per cluster**, counting what they already own: financials (XLF/BAC/KRE/C),
   broad market (SPYM), staples (KO/XLP/WMT), energy (XLE/OXY), metals (SLV/GDX), emerging
   markets (EWZ/EEM), crypto-beta (IBIT/HOOD/SOFI).
4. **The option chain decides, not the list.** `wheel-screen` still has to show 28–45 DTE,
   open interest ≥ 500, spread ≤ 10% of mid, credit ≥ $35 and no earnings in the window.
5. **Dividends are a covered-call problem.** KO, XLF, XLP and XLE pay quarterly. Before
   writing a call, check the ex-dividend date: an in-the-money call near it is likely to
   be assigned early. T, VZ, MO and KMI stay off the menu for that reason.

## Write the profile

Copy the structure of `profile.example.toml` exactly and change only the values, so the
scripts can read every key. Put everything qualitative (tax, goals, other holdings,
correlation notes, their chosen watchlist) in `[account].notes` as plain sentences.
**No account numbers, no credentials.**

Verify it loads (a profile that would loosen a guardrail is rejected) and the guardrails
still pass:

```bash
uv run python src/trading_skills/profile.py
uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py --self-check
uv run python .claude/skills/wheel-screen/scripts/wheel_screen.py --self-check
```

## Close with the numbers

From live excess liquidity and settled cash, state in dollars: the per-position cap, the
total cap, the highest strike they can sell a put at (cap ÷ 100), and the buffer that
stays outside. Confirm the collateral setting in words ("every put is 100% backed by
cash"). For example: *"At $20k you get two positions with strikes up to $60."*
Name the core tickers that fit. Then offer the first screen with `wheel-screen`.

## Switching to live

Only when the user asks, in a session after they started on paper:

1. Read the journal. If `profitable_cycles` is below 3, say so and recommend staying on
   paper. If they insist, go ahead, but record the override in `notes` with the date.
2. Re-verify the live port with `ib-account` (the id must **not** start with `DU`).
3. Set `live_trading = true`, change `IB_PORT` in `.env`, and keep the beginner sizing
   until they have earned the next row in CLAUDE.md.
