---
name: wheel-onboard
description: First-run interview that personalizes the wheel advisor and writes profile.toml. Use when profile.toml is missing, when the user says "set me up", "onboard me", "personalize", or when their account, risk tolerance or holdings change materially.
---

# Wheel Onboard

Interview the user and write `profile.toml`, which overrides `profile.example.toml`.
Ask **one topic at a time** and keep it conversational. Offer the default in brackets so
the user can just say "default". This is a conversation, not a form.

## Before asking

If `profile.toml` exists, read it and ask what changed instead of starting from scratch.

## The interview

1. **Experience.** Have they sold options before? Run the wheel? This decides how much to
   explain and whether to start with 1 contract [yes, until 3 profitable cycles].
2. **Broker connection.** TWS or IB Gateway, paper or live, which port? Set `IB_PORT` in
   `.env` (copy `env.template`). Then verify by running
   `uv run python .claude/skills/ib-account/scripts/account.py --all-accounts`
   and show them which account the port serves. **Start on paper if they are new.**
3. **Account size, type and currency.** Cash or margin account? Base currency? Read
   `net_liquidation`, `excess_liquidity` and cash live from step 2 and confirm them. If the
   base currency is not USD, note it: every cap must be converted. Record a rough `size_usd`.
   Ask about open short options too; they count toward `max_positions` and total collateral.
4. **Emergency buffer.** How much cash do they keep outside the broker, and how many
   months of spending does it cover? It must stay outside IBKR [3–6 months of spending].
5. **The rest of their net worth.** Other accounts, crypto, property, a large single stock?
   Needed for Tier 2: any name above ~10% of net worth goes in `overweight`. Ask about
   correlation too (e.g. a tech-heavy job plus tech stock).
6. **Options permissions and market data.** IBKR options level (cash-secured puts and
   covered calls need level 1–2)? OPRA subscription? Without OPRA nothing can be staged.
7. **Strategy fit.** Target DTE [30], delta band [0.20–0.30], max concurrent positions [3],
   minimum credit [$35]. Only move these if they give a reason. Explain the trade-off:
   higher delta means more premium and more assignment.
8. **Universe.** Names they would happily own 100 shares of. Start from the starter
   menu below, filtered to what fits their cap. Names never to sell puts on go in
   `excluded` (covered-call funds such as QYLD/JEPQ are excluded by default).
9. **Tax and constraints.** Ask where they are tax resident and whether anything constrains
   them (withholding on US dividends, wash-sale rules, an employer trading policy). **Record
   it in `notes`; do not give tax advice.** Suggest a professional for anything uncertain.
10. **Goal.** Income, accumulating shares cheaply, or learning? A "learning" goal favours
    paper trading and the smallest size.

## Starter menu (a conversation base, not a recommendation)

Liquid names that wheel traders commonly use. Prices change: **before showing a name, pull
its live price with `stock-quote`** and keep only those whose 0.20–0.30 delta strike
(~5–10% below spot) fits the per-position cap, i.e. strike × 100 ≤ 35% of excess liquidity
(~$7k at $20k, ~$12k at $35k, ~$17.5k at $50k). The bands below are rough collateral per
contract (late 2026).

| Collateral | ETFs (no earnings) | Stocks |
|---|---|---|
| under ~$5k | EWZ, SLV, IBIT | SOFI, PFE, NKE, BAC |
| ~$5–7k | XLF, XLE | PYPL, OXY, UBER, INTC |
| ~$7–10k | | KO, CSCO, HOOD |
| ~$10–17.5k | XBI | DIS, WMT |

Alternates: KRE or C for XLF/BAC, EEM for EWZ, GDX for SLV, XLP for KO, TLT as a rates
diversifier (when its credit clears the floor).

Rules for building their list from it:

1. **Filter by their live cap first.** Never show a $20k account names that don't fit.
2. **One name per cluster**, and count the whole book: financials (BAC/XLF/KRE/C), energy
   (XLE/OXY), metals (SLV/GDX), emerging markets (EWZ/EEM), healthcare (PFE/XBI),
   crypto-beta (IBIT/HOOD/SOFI), staples (KO/XLP). Ask what they already own; a tech-heavy
   job or portfolio counts as a cluster.
3. **At most one high-volatility slot** (SOFI, HOOD, INTC, XBI, IBIT). Fill the others
   with a no-earnings ETF or a steady large cap.
4. **The chain decides, not the list.** `wheel-screen` still has to show 21–45 DTE, open
   interest ≥ 500, spread ≤ 10% of mid, credit ≥ $35 and no earnings in the window.
5. **High dividend payers are a covered-call problem**: calls can be assigned early before
   the ex-dividend date. T, VZ, MO and KMI are off the default menu for that reason, and
   low-priced names (F, T) rarely clear the $35 credit floor anyway.

## Write the profile

Copy the structure of `profile.example.toml` exactly. Change only the values, so the
scripts can still read every key. Put everything qualitative (tax, goals, other holdings,
correlation notes) in `[account].notes` as plain sentences. **No account numbers, no
credentials.**

Then verify that it loads (a profile that would loosen a guardrail is rejected) and that
the guardrails still pass:

```bash
uv run python src/trading_skills/profile.py
uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py --self-check
uv run python .claude/skills/wheel-screen/scripts/wheel_screen.py --self-check
```

## Close with the numbers

Using the live excess liquidity and settled cash, print in dollars: the per-position cap,
the total collateral cap, the highest strike they can sell a put on (cap ÷ 100), and the
emergency buffer that stays outside. Point out any of their favourite names whose strike
range doesn't fit. Then offer the first screen with `wheel-screen`.
