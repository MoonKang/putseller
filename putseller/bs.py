"""Black-Scholes for European puts (no dividends).

Good enough to rank short-dated puts; American early-exercise premium and
dividends are ignored, so deltas are approximate.
"""
from __future__ import annotations

from math import erf, exp, log, sqrt


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + erf(x / sqrt(2.0)))


def _d1(s: float, k: float, t: float, r: float, sigma: float) -> float:
    return (log(s / k) + (r + 0.5 * sigma * sigma) * t) / (sigma * sqrt(t))


def put_price(s: float, k: float, t: float, r: float, sigma: float) -> float:
    d1 = _d1(s, k, t, r, sigma)
    d2 = d1 - sigma * sqrt(t)
    return k * exp(-r * t) * _cdf(-d2) - s * _cdf(-d1)


def put_delta(s: float, k: float, t: float, r: float, sigma: float) -> float:
    """Signed delta, between -1 and 0."""
    return _cdf(_d1(s, k, t, r, sigma)) - 1.0


def implied_vol(price: float, s: float, k: float, t: float, r: float) -> float | None:
    """Volatility that reproduces `price`, or None if no sensible answer exists."""
    if price <= 0 or s <= 0 or k <= 0 or t <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if price <= put_price(s, k, t, r, lo) or price >= put_price(s, k, t, r, hi):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if put_price(s, k, t, r, mid) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-6:
            break
    return (lo + hi) / 2
