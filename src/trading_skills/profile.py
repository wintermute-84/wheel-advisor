# ABOUTME: Loads and validates the trader's profile (guardrails, exclusions) from profile.toml.
# ABOUTME: Falls back to the committed profile.example.toml when no profile exists yet.

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPO_ROOT / "profile.toml"
EXAMPLE = REPO_ROOT / "profile.example.toml"


def _num(v) -> bool:
    """A real number. TOML booleans are ints in Python, so `true` must not pass as 1."""
    return type(v) in (int, float)


def validate(p: dict) -> dict:
    """Refuse a profile that would silently loosen a guardrail. Raises ValueError."""
    w, z, u = p["wheel"], p["sizing"], p["universe"]
    errors = []
    for k in (
        "max_total_collateral_frac",
        "max_position_collateral_frac",
        "max_cash_collateral_frac",
    ):
        if not (_num(z[k]) and 0 < z[k] <= 1):
            errors.append(f"sizing.{k} must be in (0, 1], got {z[k]!r}")
    for k in ("max_positions", "contracts_per_position"):
        if not (type(z[k]) is int and z[k] >= 1):
            errors.append(f"sizing.{k} must be an integer >= 1, got {z[k]!r}")
    for k in ("dte_band", "delta_band"):
        b = w[k]
        if not (isinstance(b, list) and len(b) == 2 and 0 <= b[0] < b[1]):
            errors.append(f"wheel.{k} must be [low, high] with low < high, got {b!r}")
    if not 0 < w["delta_band"][1] < 1:
        errors.append("wheel.delta_band must be within (0, 1)")
    if not 0 < w["max_spread_frac"] <= 1:
        errors.append("wheel.max_spread_frac must be in (0, 1]")
    for k, floor in (("min_credit", 1), ("min_open_interest", 1), ("min_underlying_price", 0)):
        if not (_num(w[k]) and w[k] >= floor):
            errors.append(f"wheel.{k} must be a number >= {floor}, got {w[k]!r}")
    for k in ("excluded", "overweight"):
        if not (isinstance(u[k], list) and all(isinstance(s, str) for s in u[k])):
            errors.append(f'universe.{k} must be a list of tickers like ["QYLD"], got {u[k]!r}')
    if errors:
        raise ValueError("invalid profile:\n  " + "\n  ".join(errors))
    return p


def load_profile(path: Path | None = None) -> dict:
    """Return the validated profile. profile.toml wins; the example is the default."""
    path = path or (PROFILE if PROFILE.exists() else EXAMPLE)
    with open(path, "rb") as f:
        return validate(tomllib.load(f))


if __name__ == "__main__":
    ex = load_profile(EXAMPLE)
    for bad in (
        {**ex, "sizing": {**ex["sizing"], "max_cash_collateral_frac": 1.5}},
        {**ex, "universe": {**ex["universe"], "excluded": "QYLD"}},
        {**ex, "wheel": {**ex["wheel"], "dte_band": [45, 21]}},
        {**ex, "sizing": {**ex["sizing"], "contracts_per_position": 0}},
        {**ex, "wheel": {**ex["wheel"], "min_credit": True}},
        {**ex, "wheel": {**ex["wheel"], "min_open_interest": 0}},
    ):
        try:
            validate(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted a bad profile: {bad}")
    print("self-check: profile validation OK")
