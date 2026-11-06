"""Replay the Nov 3 PM - Nov 5 PM sessions the desk missed while IBKR was disconnected.

Starts from the last good state (v60, Nov 3 morning) and runs plan/gut check/gate/fill/
decum/settle exactly as the scheduled runs would, using prices recovered from IBKR.
Chains for Nov 3 PM and Nov 4 AM expired out of IBKR, so those are priced at the same
moneyness as the previous day's real chain. Every plan is marked RECONSTRUCTED. Stops use the options' own IBKR price history
(optbars/) where IBKR still had it, and the engine's fallback model elsewhere.
"""
import json, math, subprocess, sys
from datetime import datetime, date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pricing as px  # noqa: E402

THETA = [sys.executable, str(ROOT / "scripts" / "theta.py")]
JEV = "/mnt/skills/plugins/vibe-classification/scripts/jev.py"
RATIO = 10.027          # S&P 500 close 7688.62 / SPY close 766.81 on Nov 4
VER = "62"              # dashboard state version we overwrite
HV30 = 0.116
LOG = []


def sh(*args, ok=(0,)):
    r = subprocess.run(THETA + list(args), cwd=HERE, capture_output=True, text=True)
    if r.returncode not in ok:
        print(r.stdout, r.stderr)
        raise SystemExit(f"FAILED: {args}")
    return r.stdout


def implied(price, S, K, T):
    lo, hi = 0.01, 5.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if px.bs_put(S, K, T, mid) > price:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2, 4)


def real_chain(rows, S_quote, quote_time, expiry):
    T = px.years_to(px.expiry_dt(expiry), datetime.fromisoformat(quote_time).astimezone(px.ET))
    return [{"strike": k, "bid": b, "ask": round(b + 0.05, 2), "iv": implied(b + 0.025, S_quote, k, T),
             "contract_id_ex": f"reconstructed-{k}"} for k, b in rows]


def moneyness_chain(plan_file, spx_new):
    p = json.load(open(ROOT / "db" / "plans" / plan_file))
    out, seen = [], set()
    for r in p["rows"]:
        if not r.get("bid"):
            continue
        k = round(spx_new * r["strike"] / p["spx"] / 5) * 5
        if k in seen:
            continue
        seen.add(k)
        out.append({"strike": k, "bid": r["bid"], "ask": r.get("ask"), "iv": r["iv"],
                    "contract_id_ex": f"estimated-{k}"})
    return out


def session(name, asof, leg, expiry, spy, aapl, chain, events, heads, decum=False):
    snap = {"asof": asof, "leg": leg, "expiry": expiry, "spy": spy, "aapl": aapl,
            "sgov": {"last": 100.43}, "chain": chain, "quote_status": "RECONSTRUCTED"}
    json.dump(snap, open(HERE / "snapshot.json", "w"))
    sh("plan", "--snapshot", "snapshot.json", "--out", "plan.json", "--final", "--state-version", VER)
    plan = json.load(open(HERE / "plan.json"))
    plan["quote_status"] = "RECONSTRUCTED"
    plan["reconstructed"] = ("Backfilled on Nov 5 after an IBKR outage; option prices from IBKR trade "
                             "history" + (" (chain estimated from the prior session's moneyness)"
                                          if "estimated" in json.dumps(chain) else "") +
                             "; headlines rebuilt from news timelines for that hour.")
    json.dump(plan, open(HERE / "plan.json", "w"))
    args = ["jev-input", "--plan", "plan.json", "--events", events]
    for h in heads:
        args += ["--headline", h]
    sh(*args)
    for i, o in (("jev_in.json", "jev.json"), ("jev_in_headlines.json", "jev_headlines.json")):
        with open(HERE / i) as fi, open(HERE / o, "w") as fo:
            subprocess.run([sys.executable, JEV], stdin=fi, stdout=fo, check=True)
    args = ["gate", "--plan", "plan.json", "--jev", "jev.json", "--jev-headlines", "jev_headlines.json",
            "--events", events]
    for h in heads:
        args += ["--headline", h]
    sh(*args, "--state-version", VER)
    sh("link", "--plan", "plan.json")
    sh("fill", "--plan", "plan.json", "--state-version", VER)
    plan = json.load(open(HERE / "plan.json"))
    g = plan["gate"]
    LOG.append({"session": name, "strike": (plan["chosen"] or {}).get("strike"),
                "otm_pct": (plan["chosen"] or {}).get("otm_pct"), "bid": (plan["chosen"] or {}).get("bid"),
                "p_ok": g.get("p_ok"), "max_shock": max([h.get("shock") or 0 for h in g.get("headline_scores", [])] or [0]),
                "A": plan["decisions"].get("A"), "B": plan["decisions"].get("B"), "fills": plan["fills"]})
    if decum:
        LOG.append({"decum": name, "out": sh("decum", "--snapshot", "snapshot.json", "--state-version", VER)[:400]})


def close(day, spy_close, aapl_close):
    json.dump({"spy": spy_close, "aapl": aapl_close, "sgov": 100.43, "spy_close": spy_close},
              open(HERE / "quotes.json", "w"))
    out = sh("settle", "--quotes", "quotes.json", "--bars", "spy_bars.json", "--now", f"{day}T16:20:00-05:00",
             "--option-bars", "optbars", "--ratio", str(RATIO), "--state-version", VER)
    LOG.append({"close": day, "out": json.loads(out)})


def spy(last, prior, open_, iv, pct):
    return {"last": last, "prior_close": prior, "open": open_, "iv": iv, "hv30": HV30, "iv_pctile_52w": pct}


def main():
    # --- Tue Nov 3, 3:02 PM: overnight put to Wed (chain from Monday's at the same moneyness)
    s = 753.87 * RATIO
    session("Nov 3 PM overnight", "2026-11-03T15:02:00-05:00", "1dte_put", "2026-11-04",
            spy(753.87, 752.10, 752.72, 0.16, 0.6), {"last": 329.94, "prior_close": 338.64},
            moneyness_chain("2026-11-02-1dte_put.json", s),
            "US midterm election results arrive tonight; ADP employment and the trade balance are released Wednesday morning; no Fed decision, CPI or jobs report before expiry",
            ["Apple sheds about $130 billion in market value as shares slide",
             "Bill Ackman says he worries the stock market is broken",
             "Americans vote in midterm elections; results expected tonight",
             "Sysco falls 9% on soft restaurant traffic and higher fuel costs",
             "Technology leads S&P 500 sectors as investors return to big tech"],
            decum=True)
    close("2026-11-03", 754.35, 331.72)

    # --- Wed Nov 4, 9:46 AM: 0DTE (chain from Tuesday morning's at the same moneyness)
    s = 767.7 * RATIO
    session("Nov 4 AM 0DTE", "2026-11-04T09:46:00-05:00", "0dte_put", "2026-11-04",
            spy(767.7, 754.35, 763.0, 0.15, 0.55), {"last": 351.66, "prior_close": 331.72},
            moneyness_chain("2026-11-03-0dte_put.json", s),
            "No Fed decision, CPI or jobs report before today's close; ADP employment already released at 8:15",
            ["Democrats win control of Congress in midterm elections",
             "Iran's Revolutionary Guard says four oil tankers caught fire after hitting mines in the Strait of Hormuz",
             "Oil slips on signs of US-Iran diplomacy",
             "Tesla and SpaceX fall premarket after election results",
             "Gold jumps nearly 3% to $4,050 an ounce",
             "CVS plans to split into two public companies"])
    # --- Wed Nov 4, 3:02 PM: overnight put to Thu (real Nov 5 chain, quotes as of ~2:47 PM)
    session("Nov 4 PM overnight", "2026-11-04T15:02:00-05:00", "1dte_put", "2026-11-05",
            spy(767.28, 754.35, 763.0, 0.15, 0.55), {"last": 352.09, "prior_close": 331.72},
            real_chain([(7325, 0.10), (7300, 0.10), (7275, 0.10), (7250, 0.10), (7200, 0.05),
                        (7150, 0.05), (7100, 0.05)], 767.6 * RATIO, "2026-11-04T14:47:00-05:00",
                       date(2026, 11, 5)),
            "Qualcomm and Arm report after the close; weekly jobless claims and productivity Thursday 8:30 AM; Bank of England decision Thursday; no Fed decision, CPI or jobs report before expiry",
            ["Dow jumps 3% as Democrats take control of Congress",
             "Brent crude falls below $100 a barrel for the first time in weeks",
             "Amazon and Microsoft surge 7% as megacaps rally",
             "FIS plunges 25% after CFO departure and downgrade",
             "Democrats on track to flip seven Senate seats in states Trump won"],
            decum=True)
    close("2026-11-04", 766.81, 349.60)

    # --- Thu Nov 5, 9:46 AM: 0DTE (real chain; option quotes 15 min delayed, i.e. ~9:31)
    session("Nov 5 AM 0DTE", "2026-11-05T09:46:00-05:00", "0dte_put", "2026-11-05",
            spy(755.96, 766.81, 765.47, 0.17, 0.70), {"last": 351.97, "prior_close": 349.60},
            real_chain([(7360, 0.10), (7355, 0.10), (7350, 0.10), (7340, 0.10), (7325, 0.10),
                        (7300, 0.05), (7250, 0.05), (7200, 0.05)], 765.0 * RATIO,
                       "2026-11-05T09:31:00-05:00", date(2026, 11, 5)),
            "No major US data or Fed decision before the 4 PM expiry; weekly jobless claims and productivity were already released at 8:30",
            ["South Korea's Kospi closes down 5.3% as SK hynix sinks 9%",
             "Chip stocks fall for a second day before the bell; Qualcomm phone chip sales drop 26%",
             "Democrats win control of Congress in midterm elections",
             "Nasdaq-100 futures down 0.6% while Dow futures rise 300 points",
             "Bank of England raises rates to 4% in 8-1 vote",
             "US jobless claims fall to 189,000; productivity grows 3.6%"])
    # --- Thu Nov 5, 3:02 PM: overnight put to Fri (real Nov 6 chain as of ~2:47 PM)
    session("Nov 5 PM overnight", "2026-11-05T15:02:00-05:00", "1dte_put", "2026-11-06",
            spy(739.84, 766.81, 765.47, 0.21, 0.93), {"last": 346.39, "prior_close": 349.60},
            real_chain([(7025, 1.7), (7000, 1.5), (6950, 1.4), (6900, 1.25), (6800, 0.85), (6600, 0.55),
                        (6400, 0.5), (6200, 0.35), (6000, 0.25), (5800, 0.2), (5600, 0.15)],
                       737.0 * RATIO, "2026-11-05T14:47:00-05:00", date(2026, 11, 6)),
            "October jobs report Friday 8:30 AM, before expiry; no Fed decision or CPI",
            ["Nasdaq falls 4.5% and drops below its 200-day average for the first time since April",
             "Nvidia erases its 2026 gain as AI chip selloff deepens",
             "VIX jumps to highest level since March",
             "Micron sinks 16% and Broadcom 9% in chip rout",
             "Democratic sweep stirs fears of limits on data-center buildout"],
            decum=True)
    close("2026-11-05", 733.00, 344.31)
    json.dump(LOG, open(HERE / "replay_log.json", "w"), indent=1, default=str)
    print(json.dumps(LOG, indent=1, default=str))


if __name__ == "__main__":
    main()
