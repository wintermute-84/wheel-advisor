#!/usr/bin/env python3
# ABOUTME: Screens cash-secured put candidates the account can actually afford.
# ABOUTME: Applies every CLAUDE.md guardrail and ranks survivors by annualized yield.

"""Rank cash-secured put candidates against the account guardrails.

Quotes and greeks come live from IB (OPRA); open interest is backfilled from
yfinance, which no longer serves usable option bid/ask or IV. Requires TWS running.

    uv run python .claude/skills/wheel-screen/scripts/wheel_screen.py \
        KO PFE F --excess-liquidity 30000

    uv run python .claude/skills/wheel-screen/scripts/wheel_screen.py --self-check
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "src"))

from trading_skills.profile import EXAMPLE, load_profile  # noqa: E402


def apply_profile(profile: dict) -> None:
    """Set the guardrail constants from a profile (see profile.example.toml).

    This screener only proposes short puts, which add exposure, so excluded names and
    names over the concentration cap (overweight) are both skipped here.
    """
    global MIN_CREDIT, MAX_POSITION_COLLATERAL_FRAC, MAX_SPREAD_FRAC, DELTA_BAND, DTE_BAND
    global MIN_UNDERLYING_PRICE, MIN_OPEN_INTEREST, EXCLUDED
    w, z, u = profile["wheel"], profile["sizing"], profile["universe"]
    MIN_CREDIT = w["min_credit"]
    MAX_SPREAD_FRAC = w["max_spread_frac"]
    DELTA_BAND = tuple(w["delta_band"])
    DTE_BAND = tuple(w["dte_band"])
    MIN_UNDERLYING_PRICE = w["min_underlying_price"]
    MIN_OPEN_INTEREST = w["min_open_interest"]
    MAX_POSITION_COLLATERAL_FRAC = z["max_position_collateral_frac"]
    EXCLUDED = {s.upper() for s in u["excluded"] + u["overweight"]}


apply_profile(load_profile())


def evaluate_put(
    *,
    strike: float,
    bid: float | None,
    ask: float | None,
    open_interest: int | None,
    delta: float | None,
    dte: int,
    spot: float,
    collateral_cap: float,
) -> tuple[dict | None, str | None]:
    """Score one put strike. Returns (candidate, None) or (None, reject_reason).

    Pure — no network. This is the tested core.
    """
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None, "no two-sided market"

    mid = (bid + ask) / 2
    credit = mid * 100
    collateral = strike * 100

    if (ask - bid) / mid > MAX_SPREAD_FRAC:
        return None, f"spread {(ask - bid) / mid:.0%} of mid"
    if credit < MIN_CREDIT:
        return None, f"credit ${credit:.0f} under floor"
    if collateral > collateral_cap:
        return None, f"collateral ${collateral:,.0f} over cap"
    if open_interest is None:
        return None, "OI unknown"
    if open_interest < MIN_OPEN_INTEREST:
        return None, f"OI {open_interest} under {MIN_OPEN_INTEREST}"
    if delta is None:
        return None, "no delta"
    if not (DELTA_BAND[0] <= abs(delta) <= DELTA_BAND[1]):
        return None, f"delta {abs(delta):.2f} off band"

    yield_pct = credit / collateral * 100
    return {
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "mid": round(mid, 2),
        "credit": round(credit, 2),
        "collateral": round(collateral, 2),
        "delta": round(abs(delta), 3),
        "dte": dte,
        "open_interest": open_interest,
        "breakeven": round(strike - mid, 2),
        "downside_pct": round((spot - (strike - mid)) / spot * 100, 2),
        "yield_pct": round(yield_pct, 2),
        "annualized_pct": round(yield_pct * 365 / dte, 1),
    }, None


def _open_interest_by_strike(symbol: str, expiry_ymd: str) -> dict[float, int]:
    """Open interest per put strike, from yfinance.

    IB's real-time tickers do not carry open interest, and Yahoo no longer serves
    usable option bid/ask or IV — so each source supplies the half the other lacks.
    Best effort: an empty map makes every strike fail the OI check, never a crash.
    """
    from trading_skills.options import get_option_chain as yf_chain

    iso = f"{expiry_ymd[:4]}-{expiry_ymd[4:6]}-{expiry_ymd[6:]}"
    try:
        chain = yf_chain(symbol, iso)
        if "error" in chain:
            return {}
        return {
            p["strike"]: p["openInterest"]
            for p in chain.get("puts", [])
            if p.get("openInterest") is not None
        }
    except Exception:
        return {}


def screen(symbols: list[str], excess_liquidity: float, port: int, verbose: bool = False) -> dict:
    import asyncio

    from trading_skills.broker.options import get_expiries, get_option_chain
    from trading_skills.earnings import get_next_earnings_date
    from trading_skills.utils import generated_at_str

    cap = excess_liquidity * MAX_POSITION_COLLATERAL_FRAC
    today = date.today()
    results, skipped = [], []

    def dte_of(expiry_ymd: str) -> int:
        return (datetime.strptime(expiry_ymd, "%Y%m%d").date() - today).days

    for raw in symbols:
        symbol = raw.upper()
        if symbol in EXCLUDED:
            skipped.append({"symbol": symbol, "reason": "on the excluded list"})
            continue

        exp_res = asyncio.run(get_expiries(symbol, port=port))
        if not exp_res.get("success"):
            skipped.append({"symbol": symbol, "reason": exp_res.get("error", "no expiries")})
            continue

        expiries = [e for e in exp_res["expiries"] if DTE_BAND[0] <= dte_of(e) <= DTE_BAND[1]]
        if not expiries:
            skipped.append(
                {"symbol": symbol, "reason": f"no expiry in {DTE_BAND[0]}-{DTE_BAND[1]} DTE"}
            )
            continue

        earnings = get_next_earnings_date(symbol)
        for expiry in expiries:
            dte = dte_of(expiry)

            # Earnings inside the window is a hard reject — an IV crush or a gap
            # turns a 25-delta put into an assignment at a price nobody wanted.
            if earnings:
                e_date = datetime.strptime(earnings, "%Y-%m-%d").date()
                if today <= e_date <= datetime.strptime(expiry, "%Y%m%d").date():
                    skipped.append(
                        {
                            "symbol": symbol,
                            "expiry": expiry,
                            "reason": f"earnings {earnings} in window",
                        }
                    )
                    continue

            chain = asyncio.run(get_option_chain(symbol, expiry, port=port))
            if not chain.get("success"):
                skipped.append({"symbol": symbol, "expiry": expiry, "reason": chain.get("error")})
                continue

            spot = chain.get("underlying_price")
            if not spot or spot < MIN_UNDERLYING_PRICE:
                skipped.append(
                    {"symbol": symbol, "reason": f"spot {spot} under ${MIN_UNDERLYING_PRICE}"}
                )
                break
            if spot * 100 > cap * 2:
                # Even a deep OTM strike would blow the collateral cap.
                skipped.append(
                    {"symbol": symbol, "reason": f"${spot:.2f} too expensive for a ${cap:,.0f} cap"}
                )
                break

            oi_map = _open_interest_by_strike(symbol, expiry)

            for put in chain["puts"]:
                greeks = put.get("greeks") or {}
                cand, reason = evaluate_put(
                    strike=put["strike"],
                    bid=put.get("bid"),
                    ask=put.get("ask"),
                    open_interest=oi_map.get(put["strike"]),
                    delta=greeks.get("delta"),
                    dte=dte,
                    spot=spot,
                    collateral_cap=cap,
                )
                if cand:
                    cand |= {
                        "symbol": symbol,
                        "expiry": expiry,
                        "spot": round(spot, 2),
                        "iv": put.get("impliedVolatility"),
                        "next_earnings": earnings,
                    }
                    results.append(cand)
                elif verbose:
                    skipped.append(
                        {
                            "symbol": symbol,
                            "expiry": expiry,
                            "strike": put["strike"],
                            "reason": reason,
                        }
                    )

    results.sort(key=lambda c: c["annualized_pct"], reverse=True)
    return {
        "generated_at": generated_at_str(),
        "data_delay": "real-time",
        "source": "ibkr quotes + yfinance open interest",
        "excess_liquidity": excess_liquidity,
        "collateral_cap": round(cap, 2),
        "candidates": results,
        "skipped": skipped,
    }


def self_check() -> int:
    """Assert the screen rejects what it should. Pinned to the example profile."""
    apply_profile(load_profile(EXAMPLE))
    assert "QYLD" in EXCLUDED
    apply_profile({**load_profile(EXAMPLE), "universe": {"excluded": [], "overweight": ["aaa"]}})
    assert EXCLUDED == {"AAA"}, "overweight names must be skipped by a put-only screen"
    apply_profile(load_profile(EXAMPLE))
    ok = dict(
        strike=13.0,
        bid=0.44,
        ask=0.46,
        open_interest=1200,
        delta=-0.25,
        dte=35,
        spot=14.77,
        collateral_cap=4200.0,
    )

    cand, reason = evaluate_put(**ok)
    assert cand and reason is None, (cand, reason)
    assert cand["credit"] == 45.0, cand
    assert cand["collateral"] == 1300.0, cand
    assert cand["breakeven"] == 12.55, cand
    # 45/1300 = 3.4615% over 35 days -> 36.1% annualized
    assert cand["annualized_pct"] == 36.1, cand

    # Tight spread, but the premium is too small to clear commissions.
    assert evaluate_put(**{**ok, "bid": 0.15, "ask": 0.16})[1].endswith("under floor")
    assert "over cap" in evaluate_put(**{**ok, "strike": 45.0, "bid": 1.40, "ask": 1.45})[1]
    assert "spread" in evaluate_put(**{**ok, "bid": 0.30, "ask": 0.60})[1]
    assert "OI" in evaluate_put(**{**ok, "open_interest": 12})[1]
    assert evaluate_put(**{**ok, "open_interest": None})[1] == "OI unknown"
    assert "off band" in evaluate_put(**{**ok, "delta": -0.55})[1]
    assert "no delta" in evaluate_put(**{**ok, "delta": None})[1]
    assert "no two-sided" in evaluate_put(**{**ok, "bid": None})[1]

    print("self-check: all screen filters OK")
    return 0


def to_table(res: dict) -> str:
    if not res["candidates"]:
        return "No candidates passed the guardrails."
    head = (
        f"{'SYM':<6}{'EXPIRY':<12}{'STRIKE':>7}{'MID':>7}{'CREDIT':>8}"
        f"{'COLL':>9}{'Δ':>6}{'DTE':>5}{'BE':>8}{'ANN%':>7}"
    )
    rows = [
        f"{c['symbol']:<6}{c['expiry']:<12}{c['strike']:>7g}{c['mid']:>7.2f}{c['credit']:>8.0f}"
        f"{c['collateral']:>9,.0f}{c['delta']:>6.2f}{c['dte']:>5}{c['breakeven']:>8.2f}{c['annualized_pct']:>7.1f}"
        for c in res["candidates"][:20]
    ]
    return "\n".join([head, "-" * len(head), *rows])


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("symbols", nargs="*", help="tickers to screen")
    p.add_argument("--excess-liquidity", type=float, help="account excess liquidity in USD")
    p.add_argument("--port", type=int, help="IB port; defaults to IB_PORT then 7497 (TWS paper)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument("--verbose", action="store_true", help="include per-strike reject reasons")
    p.add_argument("--self-check", action="store_true", help="run filter assertions, no network")
    args = p.parse_args()

    if args.self_check:
        return self_check()
    if not args.symbols or args.excess_liquidity is None:
        p.error("need symbols and --excess-liquidity")

    from trading_skills.broker.connection import default_ib_port

    res = screen(
        args.symbols, args.excess_liquidity, args.port or default_ib_port(7497), args.verbose
    )
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        print(to_table(res))
        print(
            f"\ngenerated_at: {res['generated_at']}  data_delay: {res['data_delay']}  "
            f"source: {res['source']}  collateral cap: ${res['collateral_cap']:,.0f}"
        )
        if res["skipped"]:
            print("\nskipped:")
            for s in res["skipped"][:20]:
                print(f"  {s.get('symbol', '?'):<6} {s['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
