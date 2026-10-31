#!/usr/bin/env python3
# ABOUTME: Append-only wheel trade log and per-underlying cycle P&L rollup.
# ABOUTME: Answers "is the wheel actually working" with realized cash, not impressions.

"""Log wheel actions and roll them up into realized P&L per underlying.

    journal.py log XYZ open   --right P --strike 13 --expiry 20261120 --price 0.45
    journal.py log XYZ close  --right P --strike 13 --expiry 20261120 --price 0.20
    journal.py log XYZ assigned --strike 13 --qty 1
    journal.py report

Cash convention: money in is positive, money out is negative. Commissions always
subtract. A roll is logged as a close followed by an open — no special case.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "src"))

LEDGER = REPO_ROOT / "journal" / "trades.jsonl"
ACTIONS = ("open", "close", "assigned", "called_away", "expired")


def cash_flow(entry: dict) -> float:
    """Realized cash for one ledger entry. Pure — this is the tested core.

    open         sell to open, credit in
    close        buy to close, debit out
    expired      short expired worthless, no cash moves (the credit was booked at open)
    assigned     short put assigned, pay strike x 100 x qty for the shares
    called_away  short call assigned, receive strike x 100 x qty for the shares
    """
    action = entry["action"]
    qty = entry.get("qty", 1)
    commission = entry.get("commission", 0.0)
    price = entry.get("price") or 0.0
    strike = entry.get("strike") or 0.0

    if action == "open":
        gross = price * 100 * qty
    elif action == "close":
        gross = -price * 100 * qty
    elif action == "expired":
        gross = 0.0
    elif action == "assigned":
        gross = -strike * 100 * qty
    elif action == "called_away":
        gross = strike * 100 * qty
    else:
        raise ValueError(f"unknown action: {action}")

    return round(gross - commission, 2)


def share_delta(entry: dict) -> int:
    """Shares gained (+) or lost (-) by this entry."""
    qty = entry.get("qty", 1)
    if entry["action"] == "assigned":
        return 100 * qty
    if entry["action"] == "called_away":
        return -100 * qty
    return 0


def rollup(entries: list[dict]) -> dict:
    """Per-symbol realized cash, premium collected, and net share position."""
    by_symbol: dict[str, dict] = {}
    for e in entries:
        s = by_symbol.setdefault(
            e["symbol"],
            {"realized": 0.0, "premium": 0.0, "shares": 0, "actions": 0, "open_shorts": 0},
        )
        s["realized"] = round(s["realized"] + cash_flow(e), 2)
        s["shares"] += share_delta(e)
        s["actions"] += 1
        if e["action"] == "open":
            s["premium"] = round(s["premium"] + (e.get("price") or 0.0) * 100 * e.get("qty", 1), 2)
            s["open_shorts"] += e.get("qty", 1)
        elif e["action"] in ("close", "expired", "assigned", "called_away"):
            s["open_shorts"] = max(0, s["open_shorts"] - e.get("qty", 1))

    # Cost basis only means something while shares are held.
    for s in by_symbol.values():
        s["cost_basis_per_share"] = round(-s["realized"] / s["shares"], 2) if s["shares"] else None

    total = round(sum(s["realized"] for s in by_symbol.values()), 2)
    return {"by_symbol": by_symbol, "total_realized": total}


def read_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]


def cmd_log(args) -> int:
    entry = {
        "ts": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET"),
        "symbol": args.symbol.upper(),
        "action": args.action,
        "qty": args.qty,
        "commission": args.commission,
    }
    for field in ("right", "strike", "expiry", "price", "note"):
        if getattr(args, field, None) is not None:
            entry[field] = getattr(args, field)

    entry["cash_flow"] = cash_flow(entry)

    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    print(json.dumps(entry, indent=2))
    return 0


def cmd_report(args) -> int:
    entries = read_ledger()
    if args.symbol:
        entries = [e for e in entries if e["symbol"] == args.symbol.upper()]
    if not entries:
        print("No entries logged yet.")
        return 0

    res = rollup(entries)
    if args.json:
        print(json.dumps(res, indent=2))
        return 0

    head = f"{'SYM':<8}{'REALIZED':>11}{'PREMIUM':>10}{'SHARES':>8}{'OPEN':>6}{'BASIS':>9}"
    print(head)
    print("-" * len(head))
    for sym, s in sorted(res["by_symbol"].items()):
        basis = f"{s['cost_basis_per_share']:.2f}" if s["cost_basis_per_share"] else "-"
        print(
            f"{sym:<8}{s['realized']:>11,.2f}{s['premium']:>10,.2f}"
            f"{s['shares']:>8}{s['open_shorts']:>6}{basis:>9}"
        )
    print("-" * len(head))
    print(f"{'TOTAL':<8}{res['total_realized']:>11,.2f}")
    print("\nREALIZED is cash actually moved, net of commissions. SHARES held from")
    print("assignment carry unrealized P&L that this table does not show.")
    return 0


def self_check() -> int:
    """Walk one full wheel cycle and assert the arithmetic."""
    # Sell the 13P for 0.45, $1 commission -> +$44
    open_put = {
        "symbol": "XYZ",
        "action": "open",
        "right": "P",
        "strike": 13.0,
        "price": 0.45,
        "qty": 1,
        "commission": 1.00,
    }
    assert cash_flow(open_put) == 44.00, cash_flow(open_put)

    # Buy it back at 0.20 -> -$21
    close_put = {**open_put, "action": "close", "price": 0.20}
    assert cash_flow(close_put) == -21.00, cash_flow(close_put)

    # Expiring worthless moves no cash.
    assert cash_flow({**open_put, "action": "expired"}) == -1.00

    # Assignment pays out the strike.
    assign = {"symbol": "XYZ", "action": "assigned", "strike": 13.0, "qty": 1, "commission": 0.0}
    assert cash_flow(assign) == -1300.00, cash_flow(assign)
    assert share_delta(assign) == 100

    # Called away brings the strike back in.
    called = {
        "symbol": "XYZ",
        "action": "called_away",
        "strike": 14.0,
        "qty": 1,
        "commission": 1.0,
    }
    assert cash_flow(called) == 1399.00, cash_flow(called)
    assert share_delta(called) == -100

    # Full cycle: sell 13P (+44), assigned (-1300), sell 14C (+59), called away (+1399).
    cycle = [
        open_put,
        assign,
        {
            "symbol": "XYZ",
            "action": "open",
            "right": "C",
            "strike": 14.0,
            "price": 0.60,
            "qty": 1,
            "commission": 1.00,
        },
        called,
    ]
    res = rollup(cycle)
    xyz = res["by_symbol"]["XYZ"]
    assert xyz["realized"] == 202.00, xyz  # 44 - 1300 + 59 + 1399
    assert xyz["premium"] == 105.00, xyz  # 45 + 60 gross
    assert xyz["shares"] == 0, xyz
    assert xyz["open_shorts"] == 0, xyz
    assert xyz["cost_basis_per_share"] is None, xyz

    # Mid-cycle, holding shares: basis is what the shares really cost net of premium.
    mid = rollup([open_put, assign])
    held = mid["by_symbol"]["XYZ"]
    assert held["shares"] == 100, held
    assert held["cost_basis_per_share"] == 12.56, held  # (1300 - 44) / 100
    assert held["open_shorts"] == 0, held

    print("self-check: journal arithmetic OK")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd")

    lg = sub.add_parser("log", help="append one action to the ledger")
    lg.add_argument("symbol")
    lg.add_argument("action", choices=ACTIONS)
    lg.add_argument("--right", choices=["P", "C"])
    lg.add_argument("--strike", type=float)
    lg.add_argument("--expiry", help="YYYYMMDD")
    lg.add_argument("--price", type=float, help="option price per share")
    lg.add_argument("--qty", type=int, default=1)
    lg.add_argument("--commission", type=float, default=1.00)
    lg.add_argument("--note")

    rp = sub.add_parser("report", help="realized P&L per underlying")
    rp.add_argument("--symbol")
    rp.add_argument("--json", action="store_true")

    sub.add_parser("self-check", help="run arithmetic assertions")

    args = p.parse_args()
    if args.cmd == "log":
        return cmd_log(args)
    if args.cmd == "report":
        return cmd_report(args)
    if args.cmd == "self-check":
        return self_check()
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
