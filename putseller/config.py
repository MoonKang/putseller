"""Load rules.toml and fingerprint it."""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

REQUIRED = {
    "sizing": ["max_pct_per_ticker", "max_pct_per_sector", "max_pct_collateral"],
    "entry": [
        "delta_min", "delta_max", "dte_min", "dte_max",
        "min_annualized_return_pct", "min_iv_rank",
        "exclude_earnings_in_window", "require_above_200dma",
    ],
    "liquidity": ["max_spread_pct_of_mid", "min_open_interest"],
    "risk": ["stress_drop_pct", "max_stress_loss_pct"],
    "exit": ["take_profit_pct", "time_exit_dte", "defend_delta", "max_loss_multiple"],
}


@dataclass(frozen=True)
class Rules:
    raw: dict
    version: str  # short hash of the file contents

    def __getitem__(self, section: str) -> dict:
        return self.raw[section]

    @property
    def universe(self) -> set[str]:
        return {t.upper() for t in self.raw["universe"]}

    @property
    def risk_free_rate(self) -> float:
        return float(self.raw.get("risk_free_rate", 0.04))

    @property
    def iv_min_history_days(self) -> int:
        return int(self.raw.get("iv_rank", {}).get("min_history_days", 30))


def load_rules(path: str | Path) -> Rules:
    data = Path(path).read_bytes()
    raw = tomllib.loads(data.decode())
    if "universe" not in raw:
        raise ValueError("rules: missing 'universe'")
    for section, keys in REQUIRED.items():
        for key in keys:
            if key not in raw.get(section, {}):
                raise ValueError(f"rules: missing [{section}] {key}")
    return Rules(raw=raw, version=hashlib.sha256(data).hexdigest()[:12])
