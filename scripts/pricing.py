"""Option math for vibe-theta: Black-Scholes, IV smile, calendars, probabilities.

Conventions
- Time to expiry for pricing is calendar years (hours / 8760), which matches
  how IBKR quotes implied vol on 0DTE options (checked against live marks).
- Probabilities "rn" use the market's implied vol (risk-neutral).
- Probabilities "hist" use 30-day realized vol with fat (Student-t) tails over a
  trading-time horizon, so the gap between the two is the variance risk premium
  the strategy is trying to collect.
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from statistics import NormalDist
from zoneinfo import ZoneInfo

from scipy.stats import t as student_t

ET = ZoneInfo("America/New_York")
_N = NormalDist()
N = _N.cdf
n = _N.pdf
HOURS_PER_YEAR = 365 * 24

# NYSE full-day closures. Expirations come from IBKR, so this only matters for
# variance-horizon arithmetic; extend it each year.
HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
OPEN, CLOSE = time(9, 30), time(16, 0)
SESSION_SHARE = 0.75      # share of a day's variance that happens 9:30-16:00
OVERNIGHT_SHARE = 0.25    # share that shows up as the overnight gap
EXTRA_NIGHT_SHARE = 0.05  # each extra closed calendar day (weekends, holidays)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def expiry_dt(d: date) -> datetime:
    return datetime.combine(d, CLOSE, tzinfo=ET)


def years_to(expiry: datetime, now: datetime) -> float:
    return max((expiry - now).total_seconds() / 3600.0, 0.0) / HOURS_PER_YEAR


def variance_days(now: datetime, expiry: datetime) -> float:
    """Horizon in 'trading-day variance units' (1.0 = one full normal day)."""
    now = now.astimezone(ET)
    expiry = expiry.astimezone(ET)
    units = 0.0
    d = now.date()
    cur = now
    while d <= expiry.date():
        if is_trading_day(d):
            start = max(cur, datetime.combine(d, OPEN, tzinfo=ET))
            end = min(expiry, datetime.combine(d, CLOSE, tzinfo=ET))
            if end > start:
                units += SESSION_SHARE * (end - start).total_seconds() / (6.5 * 3600)
        nd = d + timedelta(days=1)
        if nd <= expiry.date():
            # a night passes between d and nd
            if is_trading_day(d) or d == now.date():
                units += OVERNIGHT_SHARE
            if not is_trading_day(nd):
                units += EXTRA_NIGHT_SHARE
        d = nd
        cur = datetime.combine(d, time(0, 0), tzinfo=ET)
    return max(units, 1e-6)


# ---------------------------------------------------------------- Black-Scholes

def _d1d2(S, K, T, v):
    s = v * math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * v * v * T) / s
    return d1, d1 - s


def bs_put(S, K, T, v):
    if T <= 0 or v <= 0:
        return max(K - S, 0.0)
    d1, d2 = _d1d2(S, K, T, v)
    return K * N(-d2) - S * N(-d1)


def bs_call(S, K, T, v):
    return bs_put(S, K, T, v) + S - K


def put_greeks(S, K, T, v):
    """Per-share greeks. theta is $/hour, vega is $ per 1 vol point."""
    if T <= 0 or v <= 0:
        return {"delta": -1.0 if S < K else 0.0, "gamma": 0.0, "theta_hr": 0.0, "vega": 0.0}
    d1, _ = _d1d2(S, K, T, v)
    sq = math.sqrt(T)
    return {
        "delta": N(d1) - 1.0,
        "gamma": n(d1) / (S * v * sq),
        "theta_hr": -S * n(d1) * v / (2 * sq) / HOURS_PER_YEAR,
        "vega": S * n(d1) * sq / 100.0,
    }


def p_itm_rn(S, K, T, v, kind="P"):
    if T <= 0 or v <= 0:
        return float(S < K) if kind == "P" else float(S > K)
    _, d2 = _d1d2(S, K, T, v)
    return N(-d2) if kind == "P" else N(d2)


# ---------------------------------------------------------------- smile

class Smile:
    """Implied vol vs log-moneyness, linear between quotes, flat outside."""

    def __init__(self, S: float, points: list[tuple[float, float]]):
        pts = sorted((math.log(K / S), iv) for K, iv in points if iv and iv > 0)
        if not pts:
            raise ValueError("no valid IV points for smile")
        self.S = S
        self.k = [p[0] for p in pts]
        self.v = [p[1] for p in pts]

    def iv(self, K: float, S: float | None = None) -> float:
        k = math.log(K / (S or self.S))
        if k <= self.k[0]:
            return self.v[0]
        if k >= self.k[-1]:
            return self.v[-1]
        for i in range(1, len(self.k)):
            if k <= self.k[i]:
                w = (k - self.k[i - 1]) / (self.k[i] - self.k[i - 1])
                return self.v[i - 1] * (1 - w) + self.v[i] * w
        return self.v[-1]


# ---------------------------------------------------------------- fat-tail "physical" model

T_DF = 3.0


def hist_scale(hv_annual: float, var_days: float) -> float:
    """Student-t scale matching the realized-vol variance over the horizon."""
    sd = hv_annual * math.sqrt(var_days / 252.0)
    return sd * math.sqrt((T_DF - 2) / T_DF)


def p_below_hist(S, K, hv_annual, var_days):
    return float(student_t.cdf(math.log(K / S) / hist_scale(hv_annual, var_days), T_DF))


def pdf_hist(S, x, hv_annual, var_days):
    sc = hist_scale(hv_annual, var_days)
    # density of price x where log(x/S) ~ t*sc
    return float(student_t.pdf(math.log(x / S) / sc, T_DF) / (sc * x))


def p_touch(S, level, sd_log, below=True, cdf=None):
    """Reflection-principle touch probability (driftless): ~2 x P(end beyond)."""
    if (below and level >= S) or (not below and level <= S):
        return 1.0
    z = math.log(level / S) / sd_log
    p_end = (cdf or N)(z) if below else 1 - (cdf or N)(z)
    return min(1.0, 2.0 * p_end)


# ---------------------------------------------------------------- stop level

def stop_spx_level(K, T, iv_at, stop_price, S0, vol_bump_pts_per_pct):
    """Index level at which the short put's model value reaches the stop price.

    iv_at(S) gives the strike's IV when the index is at S (sticky strike plus a
    crash bump of vol_bump_pts_per_pct vol points per 1% decline from S0).
    """
    def price_at(S):
        drop_pct = max(0.0, (S0 - S) / S0 * 100.0)
        v = iv_at(S) + vol_bump_pts_per_pct * drop_pct / 100.0
        return bs_put(S, K, T, v)

    lo, hi = K * 0.85, S0
    if price_at(hi) >= stop_price:
        return hi
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if price_at(mid) >= stop_price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def rn_density(S, smile: Smile, T, grid):
    """Breeden-Litzenberger density from smile-consistent put prices."""
    out = []
    h = S * 0.0005
    for x in grid:
        p = lambda k: bs_put(S, k, T, smile.iv(k))
        d2 = (p(x + h) - 2 * p(x) + p(x - h)) / (h * h)
        out.append(max(d2, 0.0))
    return out
