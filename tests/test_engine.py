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


def check_overnight_sizing(d, w):
    """At 3 PM the morning 0DTE and last night's put both settle at today's close;
    they must not block tonight's overnight put."""
    st = json.loads((d / "state.json").read_text())
    for p in st["portfolios"].values():
        p["positions"] = [x for x in p["positions"] if x["status"] != "open"]
        for leg, exp in (("0dte_put", "2026-10-07"), ("1dte_put", "2026-10-07")):
            p["positions"].append({"id": leg, "leg": leg, "kind": "P", "strike": 7400, "expiry": exp,
                                   "qty": 1, "entry_iv": 0.3, "status": "open", "stop": 0.75,
                                   "limit": 0.9, "spx_entry": 7800, "checked_until": "2026-10-07T09:47:00-04:00"})
    (d / "state.json").write_text(json.dumps(st))
    asof = datetime.fromisoformat("2026-10-07T15:02:00-04:00").astimezone(px.ET)
    S = 774.0 * 10.02
    snap = {"asof": asof.isoformat(), "leg": "1dte_put", "expiry": "2026-10-08",
            "spy": {"last": 774.0, "prior_close": 773.0, "open": 773.5, "hv30": 0.12, "iv": 0.126},
            "aapl": {"last": 333.0, "prior_close": 333.0}, "sgov": {"last": 100.5},
            "chain": chain(S, asof, datetime(2026, 10, 8).date(), 3, 20)}
    w("snap_on.json", snap)
    out = json.loads(run("plan", "--snapshot", "snap_on.json", "--out", "plan_on.json", cwd=d))
    assert out["qty"]["A"] >= 1 and out["qty"]["B"] >= 1, out
    # chain edge: a chain that stops at the pick must ask for an extension (exit 3)
    snap["chain"] = [r for r in snap["chain"] if r["strike"] >= out["chosen"]["strike"]]
    w("snap_edge.json", snap)
    r = subprocess.run(THETA + ["plan", "--snapshot", "snap_edge.json", "--out", "plan_edge.json"],
                       cwd=d, capture_output=True, text=True)
    assert r.returncode == 3 and '"extend"' in r.stdout, r.stdout + r.stderr
    r = subprocess.run(THETA + ["fill", "--plan", "plan_edge.json"], cwd=d, capture_output=True, text=True)
    assert r.returncode != 0 and "wider chain" in r.stderr, r.stderr
    r = subprocess.run(THETA + ["plan", "--snapshot", "snap_edge.json", "--out", "plan_edge.json", "--final"],
                       cwd=d, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    print("overnight sizing and chain-edge checks passed")


def check_calendar_and_health(d):
    """The trading-day check must not depend on market data, so a disconnected
    broker can never pass for a holiday; a failed run must reach the dashboard."""
    days = {"2026-11-03": 0, "2026-11-07": 10, "2026-11-26": 10, "2026-12-25": 10,
            "2027-01-01": 10, "2027-12-31": 0, "2027-06-18": 10, "2027-03-26": 10,
            "2027-07-05": 10, "2026-07-03": 10}
    for day, code in days.items():
        r = subprocess.run(THETA + ["calendar", "--date", day], capture_output=True, text=True)
        assert r.returncode == code, (day, r.returncode, r.stdout, r.stderr)
    run("health", "--run", "close", "--status", "failed", "--reason", "IBKR connector not connected",
        "--now", "2026-11-05T16:21:00-05:00", "--outbox", "hb", cwd=d)
    man = json.loads((d / "hb" / "manifest.json").read_text())
    got = {(e["collection"], e["doc_id"]) for e in man}
    assert ("health", "current") in got and ("health", "2026-11-05-close") in got, got
    assert any(c == "events" and "run-failed" in i for c, i in got), got
    cur = json.loads(Path([e for e in man if e["doc_id"] == "current"][0]["file_path"]).read_text())
    assert cur["status"] == "failed" and cur["date"] == "2026-11-05", cur


def check_option_price_stops():
    """Stops trigger on the option's own traded prices, not the model."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import theta
    pos = {"id": "x", "strike": 7100, "kind": "P", "expiry": "2026-11-05", "stop": 1.0, "limit": 1.15,
           "stop_state": "armed", "checked_until": "2026-11-04T15:02:00-05:00"}
    def ob(rows):
        f = Path(tempfile.mkdtemp()) / "x.json"
        f.write_text(json.dumps({"chart_step": 1800, "time": [r[0] for r in rows], "open": [r[1] for r in rows],
                                 "high": [r[2] for r in rows], "low": [r[3] for r in rows], "close": [r[4] for r in rows]}))
        return theta._bars([f])
    upto = theta.parse_dt("2026-11-05T16:20:00-05:00")
    # quiet night, then the option trades through the stop mid-morning
    ev = theta._stop_from_option_bars(dict(pos), ob([("2026-11-04T19:30:00Z", .05, .05, .05, .05),
                                                     ("2026-11-05T15:00:00Z", .1, .6, .1, .45),
                                                     ("2026-11-05T15:30:00Z", .5, 2.05, .25, .75)]), upto)
    assert ev and ev["price"] == 1.15 and "option prices" in ev["how"], ev
    # gap: opens above the limit, later trades back down to it
    p2 = dict(pos)
    ev = theta._stop_from_option_bars(p2, ob([("2026-11-05T14:30:00Z", 3.0, 4.0, 2.5, 3.0),
                                              ("2026-11-05T15:00:00Z", 2.0, 2.2, 1.1, 1.3)]), upto)
    assert ev and "after gap" in ev["how"], ev
    # option never reaches the stop: no stop, whatever the index did
    ev = theta._stop_from_option_bars(dict(pos), ob([("2026-11-05T15:00:00Z", .1, .9, .05, .3)]), upto)
    assert ev is None
    # bars from before entry are ignored
    ev = theta._stop_from_option_bars(dict(pos), ob([("2026-11-04T14:30:00Z", .9, 3.0, .8, .9)]), upto)
    assert ev is None
    print("option-price stop checks passed")


def check_plan_fixes():
    """Nov 16 2026 audit: IV from mids, clean bids, sticky-strike stops, realized vol,
    forced-sale sizing."""
    import theta as th
    # IV backed out of a price reproduces that price with the engine's own spot/clock
    S, K, T = 7423.47, 6700, 24.94 / 8760
    v = px.implied_vol_put(S, K, T, 0.175)
    assert v and abs(px.bs_put(S, K, T, v) - 0.175) < 1e-6, v
    assert px.implied_vol_put(S, K, T, 0.0) is None
    # realized vol: a 4.5% day in a quiet month must show up
    closes = [700.0]
    for r in [0.003, -0.002] * 10:
        closes.append(closes[-1] * math.exp(r))
    calm = px.realized_vol(closes)
    closes[-1] = closes[-2] * math.exp(-0.045)
    assert px.realized_vol(closes) > 2.5 * calm, (calm, px.realized_vol(closes))

    d = Path(tempfile.mkdtemp())
    w = lambda name, obj: (d / name).write_text(json.dumps(obj))
    w("q0.json", {"spy": 750.0, "aapl": 340.0, "sgov": 100.5})
    run("init", "--quotes", "q0.json", "--now", "2026-11-16T09:40:00-05:00", cwd=d)
    asof = datetime.fromisoformat("2026-11-16T09:48:00-05:00").astimezone(px.ET)
    S = 750.0 * 10.02
    ch = chain(S, asof, asof.date(), 2, 8, step=5)
    # a stray quote: one far strike bids more than its neighbours closer to the money
    far = [r for r in ch if r["bid"] == 0.05]
    stray = far[len(far) // 2]
    stray["bid"], stray["ask"] = 0.10, 0.15
    # daily bars with a crash day 7 sessions ago
    t0 = datetime.fromisoformat("2026-10-15T09:30:00-04:00")
    cl, times = [760.0], [t0.isoformat()]
    for i in range(1, 25):
        cl.append(cl[-1] * math.exp(-0.045 if i == 18 else (0.004 if i % 2 else -0.004)))
        times.append((t0 + timedelta(days=i)).isoformat())
    w("spy_daily.json", {"time": times, "close": cl})
    snap = {"asof": asof.isoformat(), "leg": "0dte_put", "expiry": asof.date().isoformat(),
            "spy": {"last": 750.0, "prior_close": 756.0, "open": 752.0, "hv30": 0.137, "iv": 0.16},
            "aapl": {"last": 340.0, "prior_close": 341.0}, "sgov": {"last": 100.5},
            "chain": ch, "quote_status": "DELAYED"}
    w("snap.json", snap)
    run("plan", "--snapshot", "snap.json", "--out", "plan.json", "--final", cwd=d)
    plan = json.loads((d / "plan.json").read_text())
    assert plan["hv30"] > 0.15 and "daily closes" in plan["hv_source"], plan["hv_source"]
    row = next(r for r in plan["rows"] if r["strike"] == stray["strike"])
    assert row["bid_raw"] == 0.10 and row["bid"] == 0.05, row
    assert plan["chosen"]["strike"] != stray["strike"], plan["chosen"]
    c = plan["chosen"]
    assert c["iv_source"] == "mid" and abs(c["model"] - (c["bid_raw"] + c["ask"]) / 2) < 0.002, c
    # the strike's vol at the stop level is never below its entry vol
    sm = px.Smile(plan["spx"], [(plan["spx"] * math.exp(k), v) for k, v in zip(plan["smile"]["k"], plan["smile"]["v"])])
    assert max(sm.iv(c["strike"], c["stop_spx"]), c["iv"]) >= c["iv"]
    loose = px.stop_spx_level(c["strike"], plan["hours_left"] / 8760, lambda s_: sm.iv(c["strike"], s_),
                              c["stop_price"], plan["spx"], 2.0)
    assert c["stop_spx"] >= loose - 1e-6, (c["stop_spx"], loose)

    # forced sale: selling $D of stock does not cure a $D margin shortfall (it frees
    # only the stock's 30% margin rate); the old code left two-thirds of the hole open
    cfg = th.load_config()
    q = {"spy": 700.0, "aapl": 300.0, "sgov": 100.5}
    now = datetime.fromisoformat("2026-11-16T16:20:00-05:00").astimezone(px.ET)
    port = th.new_portfolio("B", False, cfg, 300.0, 100.5, now.date())
    port["sgov"]["shares"] = 0
    port["positions"] = [{"status": "open", "kind": "P", "strike": 7900, "qty": 1, "expiry": "2026-11-17",
                          "entry_iv": 0.3, "leg": "1dte_put"}]
    st = {"spx_spy_ratio": 10.03, "config": cfg}
    assert th.margin_req(port, q, st, now, cfg)["excess"] < -40000
    th.sweep(port, q, st, cfg, now, th.Outbox(str(d / "ob")), "B")
    assert th.margin_req(port, q, st, now, cfg)["excess"] >= 0 and port["aapl"]["shares"] > 0, port["aapl"]
    print("plan-fix checks passed")


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
    # no gut check: B trades on the hard rules, frozen A takes no new options
    g = json.loads(run("decide", "--plan", "plan1.json", cwd=d))
    assert g["decisions"]["B"]["trade"] is True, g
    assert g["decisions"]["A"]["trade"] is False and g["decisions"]["A"].get("frozen"), g
    for gone in ("gate", "jev-input"):
        r = subprocess.run(THETA + [gone], cwd=d, capture_output=True, text=True)
        assert r.returncode != 0, f"{gone} should no longer exist"
    print(run("fill", "--plan", "plan1.json", cwd=d))
    print(run("decum", "--snapshot", "snap1.json", cwd=d))

    # close: quiet rest of day
    w("b1.json", bars("2026-10-05", [(774, 773.5, 774.2), (774.2, 773.8, 774.4)], start="15:00"))
    w("q1.json", {"spy": 774.4, "aapl": aapl, "sgov": sgov})
    print(run("settle", "--quotes", "q1.json", "--bars", "b1.json", "--now", "2026-10-05T16:20:00-04:00", cwd=d))

    # next morning: 0DTE plan, only B trades
    asof2 = datetime.fromisoformat("2026-10-06T09:45:00-04:00").astimezone(px.ET)
    snap2 = dict(snap, asof=asof2.isoformat(), leg="0dte_put", expiry="2026-10-06",
                 chain=chain(S, asof2, asof2.date(), 1, 12))
    w("snap2.json", snap2)
    print(run("plan", "--snapshot", "snap2.json", "--out", "plan2.json", cwd=d))
    print(run("decide", "--plan", "plan2.json", cwd=d))
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
    check_overnight_sizing(d, w)
    check_calendar_and_health(d)
    check_option_price_stops()
    check_plan_fixes()
    man = json.loads((d / "outbox" / "manifest.json").read_text())
    print(f"outbox docs: {len(man)}  ({d})")


if __name__ == "__main__":
    main()
