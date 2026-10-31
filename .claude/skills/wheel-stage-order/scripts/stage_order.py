#!/usr/bin/env python3
# ABOUTME: Stages a single-leg short option order (CSP or covered call) as a dry run.
# ABOUTME: Prints a full risk block and transmits to IB only when --execute is passed.

"""Stage a cash-secured put or covered call against the account guardrails.

Dry run by default: qualifies the contract, pulls the live quote and greeks, runs
every guardrail in CLAUDE.md, prints the risk block, and exits WITHOUT placing an
order. Pass --execute to transmit the limit order.

    uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py \
        KO --right P --strike 62.5 --expiry 20261120 --limit 0.70

Self-check (no IB connection needed):

    uv run python .claude/skills/wheel-stage-order/scripts/stage_order.py --self-check
"""

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_skills.profile import EXAMPLE, PROFILE, load_profile  # noqa: E402

# Premium richness. These WARN rather than block: selling cheap premium is an
# expected-value problem, not a risk-of-ruin one, and a hard block would forbid
# trading through an entire low-vol regime.
MIN_IV_HV_RATIO = 1.0  # implied should exceed realized - you are paid to carry risk
MIN_IV_RANK = 30.0  # percentile of the trailing 52 weeks of implied vol


def apply_profile(profile: dict) -> None:
    """Set the guardrail constants from a profile (see profile.example.toml).

    A cash-secured put must be secured by CASH, so the per-position cap is the LESSER of
    a margin-safety limit (excess liquidity) and a cash-securing limit (settled cash).
    The hard rule on top: collateral <= settled cash, or it is not cash-secured at all.
    EXCLUDED names are never written on either side. OVERWEIGHT names block short puts
    (they add exposure) but allow covered calls (they trim it).
    """
    global MIN_CREDIT_PER_CONTRACT, MAX_TOTAL_COLLATERAL_FRAC, MAX_CASH_COLLATERAL_FRAC
    global MIN_OPEN_INTEREST, MAX_SPREAD_FRAC, DELTA_BAND, DTE_BAND, MIN_UNDERLYING_PRICE
    global MAX_CONTRACTS, EXCLUDED, OVERWEIGHT, MAX_POSITION_COLLATERAL_FRAC, MAX_POSITIONS
    global LIVE_TRADING, CASH_SECURED_FRAC
    w, z, u = profile["wheel"], profile["sizing"], profile["universe"]
    MIN_CREDIT_PER_CONTRACT = w["min_credit"]
    MIN_OPEN_INTEREST = w["min_open_interest"]
    MAX_SPREAD_FRAC = w["max_spread_frac"]
    DELTA_BAND = tuple(w["delta_band"])
    DTE_BAND = tuple(w["dte_band"])
    MIN_UNDERLYING_PRICE = w["min_underlying_price"]
    MAX_TOTAL_COLLATERAL_FRAC = z["max_total_collateral_frac"]
    MAX_CASH_COLLATERAL_FRAC = z["max_cash_collateral_frac"]
    MAX_POSITION_COLLATERAL_FRAC = z["max_position_collateral_frac"]
    MAX_POSITIONS = z["max_positions"]
    LIVE_TRADING = profile["account"]["live_trading"]
    CASH_SECURED_FRAC = z["cash_secured_frac"]
    MAX_CONTRACTS = z["contracts_per_position"]
    EXCLUDED = {s.upper() for s in u["excluded"]}
    OVERWEIGHT = {s.upper() for s in u["overweight"]}


apply_profile(load_profile())


def check_guardrails(
    *,
    symbol: str,
    right: str,
    strike: float,
    limit: float,
    qty: int,
    bid: float | None,
    ask: float | None,
    delta: float | None,
    dte: float | None,
    underlying_price: float | None,
    excess_liquidity: float | None,
    settled_cash: float | None,
    shares_held: float | None,
    open_interest: int | None = None,
    days_to_earnings: int | None = None,
    iv: float | None = None,
    hv30: float | None = None,
    iv_rank: float | None = None,
    is_roll: bool = False,
    open_put_collateral: float = 0.0,
    open_puts: int = 0,
    itm_puts: int = 0,
) -> tuple[list[str], list[str]]:
    """Return (blockers, warnings). Pure — no IB, no IO. This is the tested core.

    open_put_collateral / open_puts / itm_puts describe the short puts ALREADY open, so
    the book-level caps (total collateral, position count, no new put while one is in the
    money) are enforced here, not left to a human. A roll replaces a position rather than
    adding one, so it skips the book-level checks; check_roll() governs it.

    Blockers are the money-losing mistakes: too little credit for the commissions,
    too much capital at risk, an unfillable spread, or a covered call written
    against shares that do not exist.
    """
    blockers: list[str] = []
    warnings: list[str] = []

    credit = limit * 100 * qty
    collateral = strike * 100 * qty if right == "P" else 0.0

    if symbol.upper() in EXCLUDED:
        blockers.append(f"{symbol.upper()} is on the excluded list (profile.toml)")

    if symbol.upper() in OVERWEIGHT and right == "P":
        blockers.append(
            f"{symbol.upper()} is over the Tier 2 concentration cap; a short put would add "
            f"exposure. A covered call against shares already held is allowed"
        )

    if qty > MAX_CONTRACTS:
        blockers.append(f"qty={qty}; profile allows {MAX_CONTRACTS} contract(s) per position")

    if credit < MIN_CREDIT_PER_CONTRACT * qty:
        blockers.append(
            f"credit ${credit:.2f} < ${MIN_CREDIT_PER_CONTRACT * qty:.2f} floor "
            f"(commissions would be >3% of the trade)"
        )

    if right == "P":
        if excess_liquidity is None or settled_cash is None:
            blockers.append("could not read ExcessLiquidity/TotalCash; cannot size the position")
        else:
            # Cash already promised to open puts is not free: IB leaves it in the cash
            # balance, but assignment on those puts would spend it.
            # cash_secured_frac < 1 (margin-secured, never for beginners) reserves only part
            # of each obligation in cash; the size caps below still use the full amount.
            free_cash = settled_cash - (0.0 if is_roll else open_put_collateral * CASH_SECURED_FRAC)
            el_cap = excess_liquidity * MAX_POSITION_COLLATERAL_FRAC
            cash_cap = free_cash * MAX_CASH_COLLATERAL_FRAC / CASH_SECURED_FRAC
            cap = min(el_cap, cash_cap)
            which = "settled cash" if cash_cap <= el_cap else "excess liquidity"
            if collateral > cap:
                blockers.append(
                    f"collateral ${collateral:,.0f} > ${cap:,.0f} cap — binding limit is "
                    f"{which} ({MAX_CASH_COLLATERAL_FRAC:.0%} of ${free_cash:,.0f} free cash = "
                    f"${cash_cap:,.0f}; {MAX_POSITION_COLLATERAL_FRAC:.0%} of excess liquidity = "
                    f"${el_cap:,.0f})"
                )
            if collateral * CASH_SECURED_FRAC > free_cash:
                blockers.append(
                    f"cash needed ${collateral * CASH_SECURED_FRAC:,.0f} exceeds free settled cash "
                    f"${free_cash:,.0f} — this would NOT be cash-secured; assignment would "
                    f"require borrowing beyond your profile"
                )
            elif CASH_SECURED_FRAC < 1:
                warnings.append(
                    f"margin-secured: only {CASH_SECURED_FRAC:.0%} of the ${collateral:,.0f} "
                    f"obligation is in cash; assignment borrows the rest on margin"
                )
            if not is_roll:
                total_cap = excess_liquidity * MAX_TOTAL_COLLATERAL_FRAC
                if open_put_collateral + collateral > total_cap:
                    blockers.append(
                        f"total put collateral ${open_put_collateral + collateral:,.0f} "
                        f"(${open_put_collateral:,.0f} open + ${collateral:,.0f} new) > "
                        f"${total_cap:,.0f} ({MAX_TOTAL_COLLATERAL_FRAC:.0%} of excess liquidity)"
                    )
                if open_puts >= MAX_POSITIONS:
                    blockers.append(
                        f"{open_puts} short put(s) already open; profile allows {MAX_POSITIONS}"
                    )
                if itm_puts:
                    blockers.append(
                        f"{itm_puts} open put(s) in the money — manage those before adding risk"
                    )
    elif right == "C":
        needed = 100 * qty
        if shares_held is None:
            blockers.append("could not read share count; refusing to write a naked call")
        elif shares_held < needed:
            blockers.append(
                f"hold {shares_held:g} shares of {symbol.upper()}, need {needed} whole "
                f"shares to cover. Fractional shares cannot be delivered on assignment"
            )

    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2
        if mid > 0 and (ask - bid) / mid > MAX_SPREAD_FRAC:
            blockers.append(
                f"bid/ask {bid:.2f}/{ask:.2f} is {(ask - bid) / mid:.1%} of mid "
                f"(> {MAX_SPREAD_FRAC:.0%}); you will not get filled near mid"
            )
        # In a roll, `limit` is the NET credit across both legs, not this leg's price,
        # so comparing it to this leg's bid is meaningless. check_roll() prices the combo.
        if not is_roll and limit < bid:
            warnings.append(
                f"limit {limit:.2f} is below the bid {bid:.2f} — leaving money on the table"
            )
    else:
        blockers.append(
            "no live bid/ask. Either OPRA is not subscribed, the paper account is not sharing "
            "the live subscription, or the quote has not arrived yet. Do not set a limit from "
            "a delayed quote"
        )

    # Tier 3 liquidity: you must be able to CLOSE the position, not just open it.
    if open_interest is None:
        warnings.append("open interest unavailable — verify >= 500 manually before trading")
    elif open_interest < MIN_OPEN_INTEREST:
        blockers.append(
            f"open interest {open_interest} < {MIN_OPEN_INTEREST}; too thin to exit without "
            f"paying the spread"
        )

    # Tier 3 earnings: an IV crush or gap turns a 25-delta put into an unwanted assignment.
    if days_to_earnings is not None and dte is not None and 0 <= days_to_earnings <= dte:
        blockers.append(f"earnings in {days_to_earnings}d falls inside the {dte:.0f}-day window")

    if underlying_price is not None and underlying_price < MIN_UNDERLYING_PRICE:
        blockers.append(f"underlying ${underlying_price:.2f} < ${MIN_UNDERLYING_PRICE:.2f} minimum")

    if delta is not None:
        if not (DELTA_BAND[0] <= abs(delta) <= DELTA_BAND[1]):
            warnings.append(
                f"delta {abs(delta):.2f} outside the {DELTA_BAND[0]:.2f}-{DELTA_BAND[1]:.2f} "
                f"entry band (fine for a roll, not for a new position)"
            )
    else:
        warnings.append("no model greeks returned; delta unverified")

    if dte is not None and not (DTE_BAND[0] <= dte <= DTE_BAND[1]):
        warnings.append(f"{dte:.0f} DTE outside the {DTE_BAND[0]}-{DTE_BAND[1]} entry band")

    # Premium richness. Warnings only - see MIN_IV_HV_RATIO for why.
    if iv is not None and hv30:
        ratio = iv / hv30
        if ratio < MIN_IV_HV_RATIO:
            warnings.append(
                f"IV {iv:.1f}% is BELOW 30d realised {hv30:.1f}% (ratio {ratio:.2f}) — "
                f"you are being underpaid for the risk. Selling into strength, not fear"
            )
    if iv_rank is not None and iv_rank < MIN_IV_RANK:
        warnings.append(
            f"IV rank {iv_rank:.0f} — implied vol sits in the bottom {iv_rank:.0f}% of the "
            f"past year. Premium is thin by historical standards"
        )

    return blockers, warnings


def check_roll(
    *,
    old_expiry: str,
    new_expiry: str,
    old_short_qty: float | None,
    qty: int,
    net_credit: float,
    old_bid: float | None,
    old_ask: float | None,
    new_bid: float | None,
    new_ask: float | None,
) -> tuple[list[str], list[str]]:
    """Roll-specific guardrails. Pure - the tested core for combos.

    A roll is BUY-to-close the old short + SELL-to-open the new one, filled as one combo.
    The failure modes here are distinct from a plain open and some are silent:

    - The old leg must actually be held short. Otherwise the BUY leg OPENS a long put,
      and the combo quietly becomes a different trade.
    - It must be a net credit. CLAUDE.md: "roll out for a net credit." Paying to roll is
      a different decision and should be made explicitly, not by a typo in the sign.
    - It must roll OUT (later expiry). Rolling in or sideways is not this trade.
    """
    blockers: list[str] = []
    warnings: list[str] = []

    if old_short_qty is None:
        blockers.append("could not read positions; cannot confirm the old leg is held short")
    elif old_short_qty > -qty:
        held = "not held" if old_short_qty == 0 else f"held at {old_short_qty:+g}"
        blockers.append(
            f"old leg is {held}, need short {qty}. The BUY leg would OPEN a long put instead "
            f"of closing a short - this would not be a roll"
        )

    if new_expiry <= old_expiry:
        blockers.append(
            f"new expiry {new_expiry} is not later than old {old_expiry} - a roll must go OUT"
        )

    if net_credit <= 0:
        blockers.append(
            f"net {net_credit:+.2f} is a debit. Rolls are for a net credit; paying to roll "
            f"is a separate decision"
        )

    if None in (old_bid, old_ask, new_bid, new_ask) or min(old_bid, old_ask, new_bid, new_ask) <= 0:
        blockers.append("no live two-sided quote on both legs - cannot price the combo")
    else:
        worst = new_bid - old_ask  # sell new at bid, buy old at ask
        best = new_ask - old_bid  # sell new at ask, buy old at bid
        if net_credit > best:
            warnings.append(
                f"net credit {net_credit:.2f} exceeds the best possible combo fill {best:.2f} "
                f"- will not fill without the market moving"
            )
        elif net_credit < worst:
            warnings.append(
                f"net credit {net_credit:.2f} is below the worst-case fill {worst:.2f} - "
                f"you are giving away {worst - net_credit:.2f}"
            )

    return blockers, warnings


def roll_block(
    *,
    symbol: str,
    right: str,
    old_strike: float,
    old_expiry: str,
    new_strike: float,
    new_expiry: str,
    qty: int,
    net_credit: float,
    old_bid: float | None,
    old_ask: float | None,
    new_bid: float | None,
    new_ask: float | None,
    new_delta: float | None,
    new_iv: float | None,
    new_dte: float | None,
    new_oi: int | None,
    underlying_price: float | None,
    settled_cash: float | None,
    execute: bool = False,
) -> str:
    """Confirmation block for a roll. States the combo structure and the SIGN explicitly,
    because a sign error on a combo is the one way this tool could cost real money."""
    # IBKR applies its $1.00 minimum PER LEG, not per combo.
    commission = 2 * max(1.00, 0.65 * qty)
    collateral = new_strike * 100 * qty if right == "P" else 0.0
    sym = symbol.upper()
    header = (
        "  LIVE ORDER — ROLL   (will transmit after checks pass)"
        if execute
        else "  DRY RUN — ROLL   (nothing has been transmitted)"
    )

    def q(b, a):
        return f"{b} / {a}" if b is not None and a is not None else "n/a"

    lines = [
        "",
        "=" * 72,
        header,
        "=" * 72,
        f"  CLOSE  BUY  {qty} {sym} {old_expiry} {old_strike:g}{right}"
        f"   market {q(old_bid, old_ask)}",
        f"  OPEN   SELL {qty} {sym} {new_expiry} {new_strike:g}{right}"
        f"   market {q(new_bid, new_ask)}",
        "",
        f"  combo order       BUY {qty} @ LMT {-net_credit:.2f}",
        "  sign              negative limit on a BUY combo = you RECEIVE",
        f"  => net CREDIT     ${net_credit * 100 * qty:,.2f}"
        f"   (net ${net_credit * 100 * qty - commission:,.2f} after ~${commission:.2f} commission)",
        "",
        f"  underlying        ${underlying_price:,.2f}"
        if underlying_price
        else "  underlying        n/a",
        f"  new delta / IV    {abs(new_delta):.3f} / {new_iv:.1f}%"
        if new_delta is not None and new_iv is not None
        else "  new delta / IV    n/a",
        f"  new DTE / OI      {new_dte:.0f} / {new_oi:,}"
        if new_dte is not None and new_oi is not None
        else f"  new DTE           {new_dte:.0f}"
        if new_dte is not None
        else "  new DTE           n/a",
        f"  new collateral    ${collateral:,.2f}   (old {old_strike:g} collateral released)",
        f"  new breakeven     ${new_strike - net_credit:,.2f} on this leg alone",
    ]
    if settled_cash is not None and collateral:
        lines.append(
            f"  settled cash      ${settled_cash:,.2f}"
            f"   ({collateral / settled_cash:.0%} committed)"
        )
    lines += [
        "",
        "  VERIFY IN TWS: the order ticket must show a net CREDIT before you confirm.",
        "=" * 72,
        "",
    ]
    return "\n".join(lines)


def premium_label(iv: float | None, hv30: float | None, iv_rank: float | None) -> str:
    """thin / normal / rich. Thin means wait: premium is cheap versus history or movement."""
    if iv_rank is None or (iv is not None and hv30 and iv < hv30) or iv_rank < 30:
        return "thin"
    return "normal" if iv_rank < 50 else "rich"


def plain_summary(
    *, symbol: str, right: str, strike: float, expiry: str, limit: float, qty: int
) -> list[str]:
    """Three sentences a beginner can act on, printed above the numbers."""
    sym, n, credit = symbol.upper(), qty * 100, limit * 100 * qty
    when = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
    if right == "P":
        return [
            f"  In plain words: if {sym} closes below ${strike:g} on {when}, you buy {n} shares",
            f"  for ${strike * n:,.0f}. That cash stays locked until then. The most you can lose",
            f"  is those shares going to zero, minus the ${credit:,.0f} you collect now.",
        ]
    return [
        f"  In plain words: if {sym} closes above ${strike:g} on {when}, your {n} shares are",
        f"  sold at ${strike:g}. You keep the ${credit:,.0f} either way, but give up any gain",
        "  above the strike.",
    ]


def risk_block(
    *,
    symbol: str,
    right: str,
    strike: float,
    expiry: str,
    limit: float,
    qty: int,
    bid: float | None,
    ask: float | None,
    delta: float | None,
    iv: float | None,
    dte: float | None,
    underlying_price: float | None,
    excess_liquidity: float | None,
    settled_cash: float | None = None,
    open_interest: int | None = None,
    execute: bool = False,
    hv30: float | None = None,
    iv_rank: float | None = None,
) -> str:
    """Human-readable confirmation block. Everything needed to approve or reject."""
    credit = limit * 100 * qty
    commission = max(1.00, 0.65 * qty)
    kind = "CASH-SECURED PUT" if right == "P" else "COVERED CALL"

    if right == "P":
        collateral = strike * 100 * qty
        breakeven = strike - limit
        max_loss = collateral - credit  # underlying to zero
        max_loss_txt = f"${max_loss:,.2f}  (underlying to $0)"
    else:
        collateral = 0.0
        breakeven = (underlying_price or 0) - limit
        max_loss_txt = "capped upside; downside is the stock you already own"

    yield_pct = (credit / collateral * 100) if collateral else float("nan")
    annualized = (yield_pct * 365 / dte) if (collateral and dte) else float("nan")

    lines = [
        "",
        "=" * 68,
        (
            f"  LIVE ORDER — {kind}   (will transmit after checks pass)"
            if execute
            else f"  DRY RUN — {kind}   (nothing has been transmitted)"
        ),
        "=" * 68,
        f"  SELL {qty} {symbol.upper()} {expiry} {strike:g}{right} @ LMT {limit:.2f}",
        "",
        *plain_summary(
            symbol=symbol, right=right, strike=strike, expiry=expiry, limit=limit, qty=qty
        ),
        "",
        f"  underlying        ${underlying_price:,.2f}"
        if underlying_price
        else "  underlying        n/a",
        f"  market            {bid if bid is not None else 'n/a'}"
        f" / {ask if ask is not None else 'n/a'}",
        f"  delta             {abs(delta):.3f}" if delta is not None else "  delta             n/a",
        (
            f"  implied vol       {iv:.1f}%"
            + (f"   vs 30d realised {hv30:.1f}%  (IV/HV {iv / hv30:.2f})" if hv30 else "")
            + (f"   IV rank {iv_rank:.0f}" if iv_rank is not None else "")
            + f"   premium: {premium_label(iv, hv30, iv_rank).upper()}"
            if iv is not None
            else "  implied vol       n/a"
        ),
        f"  DTE               {dte:.0f}" if dte is not None else "  DTE               n/a",
        f"  open interest     {open_interest:,}"
        if open_interest is not None
        else "  open interest     n/a",
        "",
        f"  credit            ${credit:,.2f}",
        f"  commission (est)  ${commission:,.2f}",
        f"  net credit        ${credit - commission:,.2f}",
        f"  collateral        ${collateral:,.2f}",
        f"  breakeven         ${breakeven:,.2f}",
        f"  max loss          {max_loss_txt}",
    ]
    if collateral:
        lines += [
            f"  yield on coll.    {yield_pct:.2f}%"
            + (f"  ({annualized:.1f}% annualized)" if not math.isnan(annualized) else ""),
        ]
    if excess_liquidity is not None:
        lines += [
            "",
            f"  excess liquidity  ${excess_liquidity:,.2f}  →  "
            f"${excess_liquidity - collateral:,.2f} after fill",
        ]
    if settled_cash is not None and collateral:
        lines += [
            f"  settled cash      ${settled_cash:,.2f}  →  "
            f"${settled_cash - collateral:,.2f} if assigned  "
            f"({collateral / settled_cash:.0%} committed)",
        ]
    lines += ["=" * 68, ""]
    return "\n".join(lines)


async def stage(args) -> int:
    from ib_async import LimitOrder, Option

    from trading_skills.broker.account import fetch_excess_liquidity
    from trading_skills.broker.connection import (
        default_ib_port,
        fetch_positions,
        ib_connection,
        normalize_positions,
    )
    from trading_skills.utils import days_to_expiry, generated_at_str

    port = args.port or default_ib_port(7497)
    # Unique client ID: upstream's CLIENT_IDS run 1-99, so stay clear of them.
    async with ib_connection(port, 120, readonly=not args.execute) as ib:
        contract = Option(
            args.symbol.upper(), args.expiry, args.strike, args.right, "SMART", currency="USD"
        )
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            print(f"ERROR: could not qualify {args.symbol} {args.expiry} {args.strike}{args.right}")
            return 2
        contract = qualified[0]

        def clean(v):
            return None if v is None or (isinstance(v, float) and math.isnan(v)) else v

        # Poll rather than sleep once: OPRA quotes can take a few seconds to arrive and
        # a fixed 1s wait silently produced "no live bid/ask", blocking valid trades.
        [ticker] = await ib.reqTickersAsync(contract)
        bid = ask = None
        for _ in range(12):
            await asyncio.sleep(0.5)
            bid, ask = clean(ticker.bid), clean(ticker.ask)
            if bid is not None and ask is not None:
                break

        greeks = ticker.modelGreeks
        delta = clean(greeks.delta) if greeks else None
        iv = round(greeks.impliedVol * 100, 2) if greeks and clean(greeks.impliedVol) else None
        underlying_price = clean(greeks.undPrice) if greeks else None

        # With OPRA but no NASDAQ/NYSE equity feed, IB cannot compute model greeks (it
        # has no underlying price) even though option quotes are live. Fall back to
        # yfinance for spot and derive delta/IV from the LIVE option mid via Black-Scholes.
        # The option price itself always comes from IB; only spot is substituted.
        if underlying_price is None:
            try:
                import yfinance as yf

                underlying_price = clean(yf.Ticker(args.symbol.upper()).fast_info.get("lastPrice"))
            except Exception:
                underlying_price = None

        if delta is None and underlying_price and bid and ask:
            from trading_skills.greeks import calculate_greeks

            g = calculate_greeks(
                spot=underlying_price,
                strike=args.strike,
                option_type="put" if args.right == "P" else "call",
                dte=max(int(days_to_expiry(args.expiry)), 1),
                market_price=(bid + ask) / 2,
            )
            delta = g["greeks"].get("delta")
            iv = g.get("iv") or iv

        account = args.account
        if not account:
            accounts = ib.managedAccounts()
            account = accounts[0] if accounts else None
        excess = await fetch_excess_liquidity(ib, account) if account else None

        # Paper first: --execute only transmits to a paper account (IB ids start with DU)
        # until the user has deliberately set live_trading = true in profile.toml.
        if args.execute and not LIVE_TRADING and not (account or "").startswith("DU"):
            print(
                f"\n  BLOCKED: account {account or '?'} is not a paper account and "
                f"profile.toml has live_trading = false. Trade on paper first; switch to live "
                f"only through the wheel-onboard skill.\n"
            )
            return 1

        if args.close:
            return await _stage_close(ib, args, contract, account, bid, ask, generated_at_str)

        # Settled cash is the cash-secured test. TotalCashValue includes unsettled
        # proceeds, so use it only as a labelled fallback.
        settled_cash = None
        cash_warning = None
        if account:
            try:
                summary = await ib.accountSummaryAsync(account)
                for tag in ("SettledCash", "TotalCashValue"):
                    rows = [i for i in summary if i.tag == tag]
                    exact = [i for i in rows if i.account == account]
                    if rows:
                        settled_cash = float((exact or rows)[0].value)
                        if tag != "SettledCash":
                            cash_warning = "SettledCash unavailable; sized on TotalCashValue"
                        break
            except Exception:
                settled_cash = None

        positions = normalize_positions(await fetch_positions(ib, account))
        shares_held = None
        if args.right == "C":
            shares_held = sum(
                p["quantity"]
                for p in positions
                if p.get("symbol", "").upper() == args.symbol.upper()
                and p.get("sec_type") in ("STK", None)
            )
        short_puts = [
            p
            for p in positions
            if p.get("sec_type") == "OPT" and p.get("right") == "P" and p["quantity"] < 0
        ]
        open_put_collateral = sum(p["strike"] * 100 * -p["quantity"] for p in short_puts)
        itm_puts = 0
        for p in short_puts:
            # ponytail: yfinance spot per open put; delayed, but ITM is a coarse test.
            try:
                import yfinance as yf

                spot = yf.Ticker(p["symbol"]).fast_info.get("lastPrice")
                itm_puts += bool(spot and spot < p["strike"])
            except Exception:
                pass

        dte = days_to_expiry(args.expiry)

        # Open interest: IB real-time tickers omit it, so yfinance is the only source.
        open_interest = None
        try:
            from trading_skills.options import get_option_chain as yf_chain

            iso = f"{args.expiry[:4]}-{args.expiry[4:6]}-{args.expiry[6:]}"
            ch = yf_chain(args.symbol.upper(), iso)
            side = "puts" if args.right == "P" else "calls"
            for row in ch.get(side, []):
                if abs(row["strike"] - args.strike) < 1e-9:
                    open_interest = row.get("openInterest")
                    break
        except Exception:
            open_interest = None

        # 30-day realised volatility, close-to-close, from yfinance price history.
        hv30 = None
        try:
            import math as _m

            import numpy as _np
            import yfinance as _yf

            closes = _yf.Ticker(args.symbol.upper()).history(period="3mo")["Close"]
            rets = _np.log(closes / closes.shift(1)).dropna()
            if len(rets) >= 20:
                hv30 = float(rets.tail(30).std() * _m.sqrt(252) * 100)
        except Exception:
            hv30 = None

        # IV rank: IB serves a historical implied-vol series for the UNDERLYING via
        # whatToShow="OPTION_IMPLIED_VOLATILITY". Percentile of today within 52 weeks.
        iv_rank = None
        try:
            from ib_async import Stock as _Stock

            und = await ib.qualifyContractsAsync(_Stock(args.symbol.upper(), "SMART", "USD"))
            bars = await ib.reqHistoricalDataAsync(
                und[0],
                endDateTime="",
                durationStr="1 Y",
                barSizeSetting="1 day",
                whatToShow="OPTION_IMPLIED_VOLATILITY",
                useRTH=True,
            )
            series = [b.close for b in bars if b.close and b.close > 0]
            if len(series) >= 60 and iv:
                lo, hi = min(series), max(series)
                if hi > lo:
                    iv_rank = (iv / 100 - lo) / (hi - lo) * 100
        except Exception:
            iv_rank = None

        # Earnings inside the window is a hard reject. Funds have no earnings and pass;
        # a stock whose date cannot be loaded fails closed.
        days_to_earnings = None
        earnings_unknown = False
        try:
            from datetime import date
            from datetime import datetime as _dt

            from trading_skills.earnings import get_next_earnings_date, is_fund

            e = get_next_earnings_date(args.symbol.upper())
            if e:
                days_to_earnings = (_dt.strptime(e, "%Y-%m-%d").date() - date.today()).days
            else:
                earnings_unknown = not is_fund(args.symbol.upper())
        except Exception:
            earnings_unknown = True

        blockers, warnings = check_guardrails(
            symbol=args.symbol,
            right=args.right,
            strike=args.strike,
            limit=args.limit,
            qty=args.qty,
            bid=bid,
            ask=ask,
            delta=delta,
            dte=dte,
            underlying_price=underlying_price,
            excess_liquidity=excess,
            settled_cash=settled_cash,
            shares_held=shares_held,
            open_interest=open_interest,
            days_to_earnings=days_to_earnings,
            iv=iv,
            hv30=hv30,
            iv_rank=iv_rank,
            is_roll=bool(args.roll_from),
            open_put_collateral=open_put_collateral,
            open_puts=len(short_puts),
            itm_puts=itm_puts,
        )
        if cash_warning:
            warnings.append(cash_warning)
        if earnings_unknown:
            blockers.append(
                "next earnings date could not be loaded for this stock; check it by hand and "
                "stage only if it falls after expiry"
            )

        if args.roll_from:
            return await _stage_roll(
                ib,
                args,
                contract,
                account,
                bid,
                ask,
                delta,
                iv,
                dte,
                underlying_price,
                settled_cash,
                open_interest,
                blockers,
                warnings,
                clean,
                generated_at_str,
            )

        print(
            risk_block(
                symbol=args.symbol,
                right=args.right,
                strike=args.strike,
                expiry=args.expiry,
                limit=args.limit,
                qty=args.qty,
                bid=bid,
                ask=ask,
                delta=delta,
                iv=iv,
                dte=dte,
                underlying_price=underlying_price,
                excess_liquidity=excess,
                settled_cash=settled_cash,
                open_interest=open_interest,
                execute=args.execute,
                hv30=hv30,
                iv_rank=iv_rank,
            )
        )
        print(f"  generated_at: {generated_at_str()}")

        for w in warnings:
            print(f"  WARNING: {w}")
        for b in blockers:
            print(f"  BLOCKED: {b}")

        if blockers:
            print("\n  Refusing to stage. Fix the blockers above.\n")
            return 1

        if not args.execute:
            print("\n  Dry run only. Re-run with --execute to transmit.\n")
            return 0

        order = LimitOrder("SELL", args.qty, args.limit, tif="GTC", orderRef="wheel")
        if account:
            order.account = account
        trade = ib.placeOrder(contract, order)
        await asyncio.sleep(2)
        print(f"\n  TRANSMITTED. orderId={trade.order.orderId} status={trade.orderStatus.status}\n")
        print(
            json.dumps(
                {
                    "order_id": trade.order.orderId,
                    "symbol": args.symbol.upper(),
                    "right": args.right,
                    "strike": args.strike,
                    "expiry": args.expiry,
                    "limit": args.limit,
                    "qty": args.qty,
                    "status": trade.orderStatus.status,
                    "generated_at": generated_at_str(),
                }
            )
        )
        return 0


async def _stage_close(ib, args, contract, account, bid, ask, generated_at_str) -> int:
    """Stage a GTC buy-to-close of a short already held. Dry run unless --execute.

    The only check that matters is the one the roll uses: the leg must be held SHORT,
    otherwise this BUY would open a new long option instead of closing anything.
    """
    from ib_async import LimitOrder

    held = None
    try:
        await asyncio.sleep(1)
        held = 0.0
        for pos in ib.positions(account) if account else ib.positions():
            if pos.contract.conId == contract.conId:
                held = float(pos.position)
    except Exception:
        held = None

    blockers = check_close(held=held, qty=args.qty, limit=args.limit, ask=ask)
    sym = args.symbol.upper()
    header = "LIVE ORDER — CLOSE" if args.execute else "DRY RUN — CLOSE   (nothing transmitted)"
    print("\n" + "=" * 68)
    print(f"  {header}")
    print("=" * 68)
    leg = f"{sym} {args.expiry} {args.strike:g}{args.right}"
    print(f"  BUY {args.qty} {leg} @ LMT {args.limit:.2f} GTC")
    print(f"  market            {bid} / {ask}")
    print(f"  cost to close     ${args.limit * 100 * args.qty:,.2f} (+ ~$1 commission)")
    print("  In plain words: this buys back the option you sold. It rests until filled or")
    print("  cancelled. Cancel it in TWS before rolling the same position.")
    print("=" * 68)
    print(f"  generated_at: {generated_at_str()}")
    for b in blockers:
        print(f"  BLOCKED: {b}")
    if blockers:
        print("\n  Refusing to stage. Fix the blockers above.\n")
        return 1
    if not args.execute:
        print("\n  Dry run only. Re-run with --execute to transmit.\n")
        return 0
    order = LimitOrder("BUY", args.qty, args.limit, tif="GTC", orderRef="wheel-close")
    if account:
        order.account = account
    trade = ib.placeOrder(contract, order)
    await asyncio.sleep(2)
    print(f"\n  TRANSMITTED. orderId={trade.order.orderId} status={trade.orderStatus.status}\n")
    return 0


def check_close(*, held: float | None, qty: int, limit: float, ask: float | None) -> list[str]:
    """Blockers for a buy-to-close. Pure - tested in self_check."""
    blockers = []
    if held is None:
        blockers.append("could not read positions; cannot confirm the leg is held short")
    elif held > -qty:
        blockers.append(
            f"position is {held:+g}, need short {qty}. This BUY would OPEN a long option"
        )
    if limit <= 0:
        blockers.append("limit must be a positive debit")
    if ask is not None and ask > 0 and limit > ask:
        blockers.append(f"limit {limit:.2f} is above the ask {ask:.2f}; that overpays")
    return blockers


async def _stage_roll(
    ib,
    args,
    new_contract,
    account,
    new_bid,
    new_ask,
    new_delta,
    new_iv,
    new_dte,
    underlying_price,
    settled_cash,
    new_oi,
    blockers,
    warnings,
    clean,
    generated_at_str,
) -> int:
    """Stage a roll as one BAG combo: BUY-to-close the old short + SELL-to-open the new.

    Filled atomically, so there is no legging risk - the underlying cannot move between
    closing one leg and opening the other.
    """
    from ib_async import ComboLeg, Contract, LimitOrder, Option

    try:
        old_strike_s, old_expiry = args.roll_from.split(":")
        old_strike = float(old_strike_s)
    except ValueError:
        print(f"ERROR: --roll-from must be STRIKE:YYYYMMDD, got {args.roll_from!r}")
        return 2

    sym = args.symbol.upper()
    oq = await ib.qualifyContractsAsync(
        Option(sym, old_expiry, old_strike, args.right, "SMART", currency="USD")
    )
    if not oq:
        print(f"ERROR: could not qualify old leg {sym} {old_expiry} {old_strike:g}{args.right}")
        return 2
    old_contract = oq[0]

    [ot] = await ib.reqTickersAsync(old_contract)
    old_bid = old_ask = None
    for _ in range(12):
        await asyncio.sleep(0.5)
        old_bid, old_ask = clean(ot.bid), clean(ot.ask)
        if old_bid and old_ask and old_bid > 0 and old_ask > 0:
            break

    # The safety-critical check: is the old leg actually held short?
    old_short_qty = None
    try:
        await asyncio.sleep(1)
        old_short_qty = 0.0
        for pos in ib.positions(account) if account else ib.positions():
            if pos.contract.conId == old_contract.conId:
                old_short_qty = float(pos.position)
    except Exception:
        old_short_qty = None

    rb, rw = check_roll(
        old_expiry=old_expiry,
        new_expiry=args.expiry,
        old_short_qty=old_short_qty,
        qty=args.qty,
        net_credit=args.limit,
        old_bid=old_bid,
        old_ask=old_ask,
        new_bid=new_bid,
        new_ask=new_ask,
    )
    blockers = blockers + rb
    warnings = warnings + rw

    print(
        roll_block(
            symbol=sym,
            right=args.right,
            old_strike=old_strike,
            old_expiry=old_expiry,
            new_strike=args.strike,
            new_expiry=args.expiry,
            qty=args.qty,
            net_credit=args.limit,
            old_bid=old_bid,
            old_ask=old_ask,
            new_bid=new_bid,
            new_ask=new_ask,
            new_delta=new_delta,
            new_iv=new_iv,
            new_dte=new_dte,
            new_oi=new_oi,
            underlying_price=underlying_price,
            settled_cash=settled_cash,
            execute=args.execute,
        )
    )
    print(f"  generated_at: {generated_at_str()}")
    for w in warnings:
        print(f"  WARNING: {w}")
    for b in blockers:
        print(f"  BLOCKED: {b}")

    if blockers:
        print("\n  Refusing to stage. Fix the blockers above.\n")
        return 1
    if not args.execute:
        print("\n  Dry run only. Re-run with --execute to transmit.\n")
        return 0

    bag = Contract(symbol=sym, secType="BAG", currency="USD", exchange="SMART")
    bag.comboLegs = [
        ComboLeg(conId=old_contract.conId, ratio=1, action="BUY", exchange="SMART"),
        ComboLeg(conId=new_contract.conId, ratio=1, action="SELL", exchange="SMART"),
    ]
    # IBKR combo convention: price = sum(BUY legs) - sum(SELL legs). Closing at ~0.60
    # and opening at ~1.40 nets -0.80, so a BUY combo at a NEGATIVE limit receives credit.
    order = LimitOrder("BUY", args.qty, -abs(args.limit), tif="GTC", orderRef="wheel-roll")
    if account:
        order.account = account
    trade = ib.placeOrder(bag, order)
    await asyncio.sleep(2)
    print(
        f"\n  TRANSMITTED ROLL. orderId={trade.order.orderId} status={trade.orderStatus.status}\n"
    )
    print(
        json.dumps(
            {
                "order_id": trade.order.orderId,
                "type": "roll",
                "symbol": sym,
                "close": f"{old_expiry} {old_strike:g}{args.right}",
                "open": f"{args.expiry} {args.strike:g}{args.right}",
                "net_credit": args.limit,
                "qty": args.qty,
                "status": trade.orderStatus.status,
                "generated_at": generated_at_str(),
            }
        )
    )
    return 0


def self_check() -> int:
    """Assert the guardrails catch the mistakes that actually cost money.

    Pinned to the example profile plus one overweight name, so a personal profile.toml
    cannot change what is asserted here.
    """
    ex = load_profile(EXAMPLE)
    apply_profile({**ex, "universe": {**ex["universe"], "overweight": ["BIGCO"]}})
    base = dict(
        symbol="ABC",
        right="P",
        strike=13.0,
        limit=0.45,
        qty=1,
        bid=0.44,
        ask=0.46,
        delta=-0.25,
        dte=35,
        underlying_price=14.77,
        excess_liquidity=20000.00,
        settled_cash=5000.00,
        shares_held=None,
        open_interest=800,
        days_to_earnings=None,
    )

    blockers, warnings = check_guardrails(**base)
    assert blockers == [], f"clean CSP should pass, got {blockers}"
    assert warnings == [], f"clean CSP should not warn, got {warnings}"

    # Credit below the commission floor.
    b, _ = check_guardrails(**{**base, "limit": 0.20})
    assert any("floor" in x for x in b), b

    # Cash binds first here: 95% of $5,000 = $4,750 vs 30% of $20,000 = $6,000.
    b, _ = check_guardrails(**{**base, "strike": 50.0, "limit": 1.50})
    assert any("binding limit is settled cash" in x for x in b), b

    # Excess liquidity binds when cash is plentiful: 30% of $20,000 = $6,000.
    b, _ = check_guardrails(**{**base, "strike": 80.0, "limit": 2.50, "settled_cash": 50000.0})
    assert any("binding limit is excess liquidity" in x for x in b), b

    # Collateral above settled cash entirely is not cash-secured at all.
    b, _ = check_guardrails(
        **{
            **base,
            "strike": 90.0,
            "limit": 3.00,
            "settled_cash": 5000.0,
            "excess_liquidity": 200000.0,
        }
    )
    assert any("NOT be cash-secured" in x for x in b), b

    # Unfillable spread.
    b, _ = check_guardrails(**{**base, "bid": 0.30, "ask": 0.60})
    assert any("bid/ask" in x for x in b), b

    # Covered call against fractional shares — they cannot be delivered on assignment.
    b, _ = check_guardrails(
        **{**base, "right": "C", "strike": 16.0, "limit": 0.50, "shares_held": 99.5}
    )
    assert any("Fractional shares" in x for x in b), b

    # Covered call with a real lot passes.
    b, _ = check_guardrails(
        **{**base, "right": "C", "strike": 16.0, "limit": 0.50, "shares_held": 100}
    )
    assert b == [], b

    # Excluded underlying — blocked on both sides.
    b, _ = check_guardrails(**{**base, "symbol": "QYLD"})
    assert any("excluded" in x for x in b), b
    b, _ = check_guardrails(**{**base, "symbol": "QYLD", "right": "C", "shares_held": 100})
    assert any("excluded" in x for x in b), b

    # Overweight name: short put blocked (adds exposure)...
    b, _ = check_guardrails(**{**base, "symbol": "BIGCO"})
    assert any("concentration cap" in x for x in b), b
    # ...but a covered call against real shares is allowed (trims exposure).
    b, _ = check_guardrails(
        **{
            **base,
            "symbol": "BIGCO",
            "right": "C",
            "strike": 16.0,
            "limit": 0.50,
            "shares_held": 100,
        }
    )
    assert b == [], b

    # A name that is merely not overweight is sized by cash, not by concentration.
    b, _ = check_guardrails(**{**base, "strike": 43.0, "limit": 1.28, "settled_cash": 4000.0})
    assert any("binding limit is settled cash" in x for x in b), b
    assert not any("concentration" in x for x in b), b

    # Contracts above the profile limit are blocked.
    b, _ = check_guardrails(**{**base, "qty": 2, "limit": 0.45})
    assert any("contract(s) per position" in x for x in b), b

    # Thin open interest blocks: you must be able to exit, not just enter.
    b, _ = check_guardrails(**{**base, "open_interest": 100})
    assert any("open interest 100" in x for x in b), b

    # Missing OI warns rather than blocks - it is a yfinance best-effort lookup.
    b, w = check_guardrails(**{**base, "open_interest": None})
    assert b == [] and any("open interest unavailable" in x for x in w), (b, w)

    # Earnings inside the DTE window is a hard reject; outside it passes.
    b, _ = check_guardrails(**{**base, "open_interest": 800, "days_to_earnings": 20})
    assert any("earnings in 20d" in x for x in b), b
    b, _ = check_guardrails(**{**base, "open_interest": 800, "days_to_earnings": 40})
    assert b == [], b

    # $4,500 put against $5,000 cash fits (95% cap = $4,750). Must pass.
    live = check_guardrails(
        symbol="XYZ",
        right="P",
        strike=45.0,
        limit=0.81,
        qty=1,
        bid=0.80,
        ask=0.81,
        delta=-0.25,
        dte=32,
        underlying_price=49.00,
        excess_liquidity=20000.00,
        settled_cash=5000.00,
        shares_held=None,
        open_interest=900,
        days_to_earnings=None,
    )
    assert live == ([], []), live

    # $4,600 still fits the 95% cash cap ($4,750) with little room to spare.
    live46 = check_guardrails(
        symbol="XYZ",
        right="P",
        strike=46.0,
        limit=1.08,
        qty=1,
        bid=1.07,
        ask=1.09,
        delta=-0.28,
        dte=32,
        underlying_price=49.00,
        excess_liquidity=20000.00,
        settled_cash=5000.00,
        shares_held=None,
        open_interest=900,
        days_to_earnings=None,
    )
    assert live46 == ([], []), live46

    # --- book-level caps: the open puts count against the new one --------------------
    # Total: 50% of $20,000 = $10,000. $9,000 open + $1,300 new breaches it.
    b, _ = check_guardrails(**{**base, "open_put_collateral": 9000.0, "settled_cash": 50000.0})
    assert any("total put collateral" in x for x in b), b
    # Position count: the profile allows 2.
    b, _ = check_guardrails(**{**base, "open_puts": 2, "open_put_collateral": 2000.0})
    assert any("already open" in x for x in b), b
    # An open put in the money blocks adding risk.
    b, _ = check_guardrails(
        **{**base, "open_puts": 1, "itm_puts": 1, "open_put_collateral": 2000.0}
    )
    assert any("in the money" in x for x in b), b
    # Cash promised to open puts is not free: $5,000 cash - $4,000 open = $1,000 free.
    b, _ = check_guardrails(**{**base, "open_puts": 1, "open_put_collateral": 4000.0})
    assert any("NOT be cash-secured" in x for x in b), b
    # A roll replaces a position, so the book-level checks do not apply to it.
    b, _ = check_guardrails(
        **{**base, "open_puts": 2, "itm_puts": 1, "open_put_collateral": 4000.0, "is_roll": True}
    )
    assert not any("already open" in x or "in the money" in x for x in b), b

    # Margin-secured profile (never for beginners): $9,000 obligation, 50% in cash.
    apply_profile(
        {
            **ex,
            "sizing": {**ex["sizing"], "cash_secured_frac": 0.5},
            "universe": {**ex["universe"], "overweight": ["BIGCO"]},
        }
    )
    b, w = check_guardrails(
        **{
            **base,
            "strike": 90.0,
            "limit": 2.0,
            "settled_cash": 5000.0,
            "excess_liquidity": 50000.0,
        }
    )
    assert not any("cash-secured" in x for x in b) and any("margin-secured" in x for x in w), (b, w)
    b, _ = check_guardrails(
        **{
            **base,
            "strike": 90.0,
            "limit": 2.0,
            "settled_cash": 4000.0,
            "excess_liquidity": 50000.0,
        }
    )
    assert any("NOT be cash-secured" in x for x in b), b
    apply_profile({**ex, "universe": {**ex["universe"], "overweight": ["BIGCO"]}})

    # --- buy-to-close -------------------------------------------------------------
    assert check_close(held=-1.0, qty=1, limit=0.22, ask=0.25) == []
    assert any("OPEN a long" in x for x in check_close(held=0.0, qty=1, limit=0.22, ask=0.25))
    assert any("OPEN a long" in x for x in check_close(held=1.0, qty=1, limit=0.22, ask=0.25))
    assert any("cannot confirm" in x for x in check_close(held=None, qty=1, limit=0.2, ask=0.25))
    assert any("overpays" in x for x in check_close(held=-1.0, qty=1, limit=0.40, ask=0.25))

    # --- premium label: thin means wait ----------------------------------------------
    assert premium_label(iv=30.0, hv30=25.0, iv_rank=60.0) == "rich"
    assert premium_label(iv=30.0, hv30=25.0, iv_rank=40.0) == "normal"
    assert premium_label(iv=30.0, hv30=25.0, iv_rank=20.0) == "thin"
    assert premium_label(iv=20.0, hv30=25.0, iv_rank=60.0) == "thin"  # below realised vol
    assert premium_label(iv=30.0, hv30=25.0, iv_rank=None) == "thin"  # unknown = thin

    # --- the plain-language block names the real consequence ------------------------
    text = "\n".join(
        plain_summary(symbol="abc", right="P", strike=13.0, expiry="20261218", limit=0.45, qty=1)
    )
    assert "buy 100 shares" in text and "$1,300" in text and "$45" in text, text

    # No market data at all.
    b, _ = check_guardrails(**{**base, "bid": None, "ask": None})
    assert any("OPRA" in x for x in b), b

    # Delta outside the band warns but does not block.
    b, w = check_guardrails(**{**base, "delta": -0.55})
    assert b == [] and any("delta" in x for x in w), (b, w)

    # --- roll guardrails ---------------------------------------------------------
    # Old Dec 40P quoted 0.60/0.61, new Jan 40P 1.40/1.41.
    roll = dict(
        old_expiry="20261218",
        new_expiry="20270115",
        old_short_qty=-1.0,
        qty=1,
        net_credit=0.79,
        old_bid=0.60,
        old_ask=0.61,
        new_bid=1.40,
        new_ask=1.41,
    )
    b, w = check_roll(**roll)
    assert b == [] and w == [], (b, w)  # 0.79 = 1.40 - 0.61, exactly the worst-case fill

    # THE silent failure: old leg not held -> the BUY leg would OPEN a long put.
    b, _ = check_roll(**{**roll, "old_short_qty": 0.0})
    assert any("would OPEN a long put" in x for x in b), b
    b, _ = check_roll(**{**roll, "old_short_qty": 1.0})  # held LONG
    assert any("would OPEN a long put" in x for x in b), b
    b, _ = check_roll(**{**roll, "old_short_qty": None})
    assert any("cannot confirm" in x for x in b), b

    # A net debit is not a roll for credit.
    b, _ = check_roll(**{**roll, "net_credit": -0.10})
    assert any("is a debit" in x for x in b), b
    b, _ = check_roll(**{**roll, "net_credit": 0.0})
    assert any("is a debit" in x for x in b), b

    # Must roll OUT.
    b, _ = check_roll(**{**roll, "new_expiry": "20261211"})
    assert any("must go OUT" in x for x in b), b
    b, _ = check_roll(**{**roll, "new_expiry": "20261218"})
    assert any("must go OUT" in x for x in b), b

    # No quotes on either leg blocks.
    b, _ = check_roll(**{**roll, "old_ask": None})
    assert any("no live two-sided quote" in x for x in b), b
    b, _ = check_roll(**{**roll, "new_bid": -1.0})  # IB's closed-market sentinel
    assert any("no live two-sided quote" in x for x in b), b

    # Fill realism: best possible = 1.41 - 0.60 = 0.81, worst = 1.40 - 0.61 = 0.79.
    b, w = check_roll(**{**roll, "net_credit": 0.90})
    assert b == [] and any("will not fill" in x for x in w), (b, w)
    b, w = check_roll(**{**roll, "net_credit": 0.70})
    assert b == [] and any("giving away" in x for x in w), (b, w)

    # In roll mode the new leg's bid must not be compared to the NET credit.
    b, w = check_guardrails(
        **{
            **base,
            "symbol": "XYZ",
            "strike": 40.0,
            "limit": 0.79,
            "bid": 1.40,
            "ask": 1.41,
            "is_roll": True,
        }
    )
    assert not any("below the bid" in x for x in w), w

    apply_profile(load_profile())
    print("self-check: all guardrails OK")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("symbol", nargs="?", help="underlying ticker")
    p.add_argument("--right", choices=["P", "C"], help="P = cash-secured put, C = covered call")
    p.add_argument("--strike", type=float)
    p.add_argument("--expiry", help="YYYYMMDD")
    p.add_argument("--limit", type=float, help="limit price per share (credit)")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--account")
    p.add_argument("--port", type=int, help="IB port; defaults to IB_PORT then 7497 (TWS paper)")
    p.add_argument(
        "--roll-from",
        metavar="STRIKE:YYYYMMDD",
        help="roll mode: buy-to-close this existing short (same right) while selling the new "
        "leg, as one combo. --limit becomes the NET credit",
    )
    p.add_argument(
        "--close",
        action="store_true",
        help="buy-to-close an existing short (same symbol/right/strike/expiry) as a GTC limit "
        "at --limit; use half the credit received for the 50%% take-profit",
    )
    p.add_argument("--execute", action="store_true", help="transmit the order (default: dry run)")
    p.add_argument("--self-check", action="store_true", help="run guardrail assertions, no IB")
    args = p.parse_args()

    if args.self_check:
        return self_check()

    missing = [f for f in ("symbol", "right", "strike", "expiry", "limit") if not getattr(args, f)]
    if missing:
        p.error(f"missing required: {', '.join('--' + m if m != 'symbol' else m for m in missing)}")

    if args.execute and not PROFILE.exists():
        p.error("no profile.toml - run the wheel-onboard skill before trading for real")

    try:
        return asyncio.run(stage(args))
    except ConnectionError as e:
        print(
            f"{e}\nStart TWS or IB Gateway, log in, and enable the API (README > Setup). "
            "Then run this again."
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
