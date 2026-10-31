# ABOUTME: Trading analysis library for market data, options, and portfolio management.
# ABOUTME: Also quiets third-party log/warning noise so terminal output stays readable.

import logging
import os
import re
import sys
import warnings
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wheel-advisor")
except PackageNotFoundError:
    __version__ = "unknown"

# IB error codes that only mean "no live data subscription for this contract".
NO_DATA_CODES = {354, 10089, 10090, 10091, 10167, 10168, 10197}
_CODE = re.compile(r"(?:Error|Warning) (\d+),")


class _QuietIB(logging.Filter):
    """Drop connection chatter the scripts already report, and fold the per-contract
    market-data errors into one plain note per run. Other IB errors pass through."""

    def __init__(self):
        super().__init__()
        self.noted = False

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if msg.startswith(("API connection failed", "Make sure API port")):
            return False  # every script prints its own "could not connect" line
        m = _CODE.match(msg)
        if m and int(m.group(1)) in NO_DATA_CODES:
            if self.noted:
                return False
            self.noted = True
            record.msg, record.args = (
                "note: IB has no live market data for some contracts (needs the OPRA "
                "subscription, and on paper accounts 'share market data' enabled). "
                "Quotes may be missing; see README > Market data.",
                (),
            )
        else:
            record.msg, record.args = f"IB: {msg}", ()
        return True


def _quiet() -> None:
    # yfinance installs its own "default" filter for its deprecation warnings on import;
    # import it first so this "ignore" filter lands in front of it.
    import yfinance  # noqa: F401

    warnings.filterwarnings("ignore", module=r"(yfinance|pandas|pandas_market_calendars)(\.|$)")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(_QuietIB())
    log = logging.getLogger("ib_async")
    log.addHandler(handler)
    log.setLevel(logging.WARNING)
    log.propagate = False


if not os.environ.get("WHEEL_DEBUG"):
    _quiet()
