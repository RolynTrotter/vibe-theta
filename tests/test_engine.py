"""End-to-end check on synthetic data: calm day, then a crash day.

Run: python3 tests/test_engine.py
"""
import json
import math
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pricing as px  # noqa: E402

THETA = [sys.executable, str(ROOT / "scripts" / "theta.py")]


def run(*args, cwd):
    out = subprocess.run(THETA + list(args), cwd=cwd, capture_output=True, text=True)
    if out.returncode:
        print(out.stdout, out.stderr)
        raise SystemExit(f"FAILED: {' '.join(args)}")
    return out.stdout


def smile_iv(m):  # m = K/S
    return min(0.11 + 12.0 * max(0.0, 1 - m), 0.9) + 0.6 * max(0.0, m - 1)


def chain(S, asof, expiry, lo_pct, hi_pct, step=25):
    T = px.years_to(px.expiry_dt(expiry), asof)
    rows = []
    k = math.floor(S * (1 - hi_pct / 100) / step) * step
    while k <= S * (1 - lo_pct / 100):
        iv = smile_iv(k / S)
        p = px.bs_put(S, k, T, iv)
        bid = max(math.floor(p / 0.05) * 0.05, 0.0)
        rows.append({"strike": k, "bid": round(bid, 2), "ask": round(bid + 0.05, 2), "iv": round(iv, 4),
                     "contract_id_ex": f"{900000 + k}@SMART"})
        k += step
    return rows


def bars(day, path, start="09:30"):
    t = datetime.fromisoformat(f"{day}T{start}:00-04:00")
    out = {"chart_step": 1800, "time": [], "open": [], "high": [], "low": [], "close": []}
    for i, (o, l, c) in enumerate(path):
        out["time"].append((t + timedelta(minutes=30 * i)).isoformat())
        out["open"].append(o); out["low"].append(l); out["close"].append(c); out["high"].append(max(o, c))
    return out


def main():
    d = Path(tempfile.mkdtemp())
    w = lambda name, obj: (d / name).write_text(json.dumps(obj))
    spy, aapl, sgov = 774.0, 333.0, 100.5
    w("q0.json", {"spy": spy, "aapl": aapl, "sgov": sgov})
    print(run("init", "--quotes", "q0.json", "--now", "2026-10-05T15:00:00-04:00", cwd=d))

    # afternoon: 1DTE put for tomorrow
    asof = datetime.fromisoformat("2026-10-05T15:00:00-04:00").astimezone(px.ET)
    exp = px.next_trading_day(asof.date())
    S = spy * 10.02
    snap = {"asof": asof.isoformat(), "leg": "1dte_put", "expiry": exp.isoformat(),
            "spy": {"last": spy, "prior_close": 770.0, "open": 769.7, "hv30": 0.12, "iv": 0.126, "iv_pctile_52w": 0.3},
            "aapl": {"last": aapl, "prior_close": 335.0}, "sgov": {"last": sgov},
            "chain": chain(S, asof, exp, 3, 20), "quote_status": "DELAYED"}
    w("snap1.json", snap)
    print(run("plan", "--snapshot", "snap1.json", "--out", "plan1.json", cwd=d))
    w("jev_ok.json", [{"id": "x", "answers": {"ok": {"type": "noul", "noul": 0.66}}}])
    # headline veto: market check passes, one headline scores as a shock
    print(run("jev-input", "--plan", "plan1.json", "--events", "Nothing major.",
              "--headline", "Record highs again", "--headline", "China launches amphibious assault on Taiwan", cwd=d))
    w("jev_hl.json", [{"id": "h1", "answers": {"shock": {"type": "noul", "noul": 0.1}}},
                      {"id": "h2", "answers": {"shock": {"type": "noul", "noul": 0.88}}}])
    out = run("gate", "--plan", "plan1.json", "--jev", "jev_ok.json", "--jev-headlines", "jev_hl.json",
              "--headline", "Record highs again", "--headline", "China launches amphibious assault on Taiwan", cwd=d)
    g = json.loads(out)
    assert g["decisions"]["A"]["trade"] is False and "headline veto" in g["decisions"]["A"]["reason"], g
    assert g["decisions"]["B"]["trade"] is True
    # all calm: both trade
    w("jev_hl.json", [{"id": "h1", "answers": {"shock": {"type": "noul", "noul": 0.1}}}])
    print(run("gate", "--plan", "plan1.json", "--jev", "jev_ok.json", "--jev-headlines", "jev_hl.json",
              "--headline", "Record highs again", cwd=d))
    print(run("fill", "--plan", "plan1.json", cwd=d))
    print(run("decum", "--snapshot", "snap1.json", cwd=d))

    # close: quiet rest of day
    w("b1.json", bars("2026-10-05", [(774, 773.5, 774.2), (774.2, 773.8, 774.4)], start="15:00"))
    w("q1.json", {"spy": 774.4, "aapl": aapl, "sgov": sgov})
    print(run("settle", "--quotes", "q1.json", "--bars", "b1.json", "--now", "2026-10-05T16:20:00-04:00", cwd=d))

    # next morning: 0DTE plan, gut check says no -> only B trades
    asof2 = datetime.fromisoformat("2026-10-06T09:45:00-04:00").astimezone(px.ET)
    snap2 = dict(snap, asof=asof2.isoformat(), leg="0dte_put", expiry="2026-10-06",
                 chain=chain(S, asof2, asof2.date(), 1, 12))
    w("snap2.json", snap2)
    print(run("plan", "--snapshot", "snap2.json", "--out", "plan2.json", cwd=d))
    w("jev_no.json", [{"id": "x", "answers": {"ok": {"type": "noul", "noul": 0.2}}}])
    print(run("gate", "--plan", "plan2.json", "--jev", "jev_no.json", cwd=d))
    print(run("link", "--plan", "plan2.json", cwd=d))
    print(run("fill", "--plan", "plan2.json", cwd=d))
    print(run("stage", "--plan", "plan2.json", "--mode", "test", cwd=d))

    # crash: SPY gaps -3% then slides to -9%
    path = [(774, 748, 749), (749, 735, 736), (736, 712, 714), (714, 705, 708)] + [(708, 704, 706)] * 8
    w("b2.json", bars("2026-10-06", [(774, 773, 774)] + path, start="09:30"))
    w("q2.json", {"spy": 706.0, "aapl": 290.0, "sgov": sgov, "spy_close": 706.0})
    out = run("settle", "--quotes", "q2.json", "--bars", "b2.json", "--now", "2026-10-06T16:20:00-04:00", cwd=d)
    print(out)
    w("snap3.json", dict(snap2, asof="2026-10-06T15:00:00-04:00", spy={"last": 706.0, "prior_close": 774.4},
                         aapl={"last": 290.0, "prior_close": 333.0}))
    print(run("decum", "--snapshot", "snap3.json", cwd=d))
    print(run("report", cwd=d))
    man = json.loads((d / "outbox" / "manifest.json").read_text())
    print(f"outbox docs: {len(man)}  ({d})")


if __name__ == "__main__":
    main()
