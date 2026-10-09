#!/usr/bin/env python3
"""vibe-theta engine: plan, gate, fill, decumulate, settle and stage far-OTM SPX
option trades for two paper portfolios (A = gut-checked, B = always).

Everything that talks to IBKR, the classifier or the dashboard is done by
Claude following SKILL.md; this script only does arithmetic on JSON files.
Every command that changes something writes database documents to an
outbox directory plus outbox/manifest.json, which Claude sends to the
dashboard in one ArtifactData batch.

Commands
  init     create state from starting quotes
  plan     read a chain snapshot, pick strikes, size, compute probability space
  gate     attach the classifier result and decide per portfolio
  fill     paper-fill the plan into each portfolio
  decum    AAPL decumulation (down-day tranches, crash tranches, pace backstop)
  settle   simulate stops through price bars, settle expiries, sweep cash, mark
  stage    print create_order_instruction arguments (test or live)
  report   print a short text summary of state
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pricing as px  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- io helpers

def load(path):
    with open(path) as f:
        return json.load(f)


def dump(obj, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, default=str)


def parse_dt(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=px.ET)
    return d.astimezone(px.ET)


def r2(x):
    return None if x is None else round(float(x), 2)


def r4(x):
    return None if x is None else round(float(x), 4)


class Outbox:
    """Collects dashboard database writes for one command."""

    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        mf = self.dir / "manifest.json"
        self.entries = load(mf) if mf.exists() else []

    def put(self, collection, doc_id, data, if_version=None, op="set"):
        fname = f"{collection.replace('/', '_')}__{doc_id}.json"
        dump(data, self.dir / fname)
        self.entries = [e for e in self.entries
                        if not (e["collection"] == collection and e["doc_id"] == doc_id)]
        e = {"op": op, "collection": collection, "doc_id": doc_id,
             "file_path": str((self.dir / fname).resolve())}
        if if_version:
            e["if_version"] = int(if_version)
        self.entries.append(e)

    def save(self):
        dump(self.entries, self.dir / "manifest.json")


def load_config(path=None):
    cfg = load(ROOT / "config.default.json")
    if path and os.path.exists(path):
        _deep_update(cfg, load(path))
    return cfg


def _deep_update(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _deep_update(a[k], v)
        else:
            a[k] = v


def event_id(ts, port, kind, extra=""):
    return f"{ts.strftime('%Y%m%dT%H%M')}-{port}-{kind}{('-' + extra) if extra else ''}"


# ---------------------------------------------------------------- portfolio math

def spx_from(state, spy):
    return spy * state["spx_spy_ratio"]


def option_value(pos, spx, now):
    T = px.years_to(px.expiry_dt(date.fromisoformat(pos["expiry"])), now)
    v = pos["entry_iv"]
    if pos["kind"] == "P":
        return px.bs_put(spx, pos["strike"], T, v)
    return px.bs_call(spx, pos["strike"], T, v)


def nlv(port, quotes, state, now):
    spx = spx_from(state, quotes["spy"])
    opt = sum(option_value(p, spx, now) * 100 * p["qty"]
              for p in port["positions"] if p["status"] == "open")
    aapl = port["aapl"]["shares"] * quotes["aapl"]
    spy = sum(l["shares"] for l in port["spy_lots"]) * quotes["spy"]
    sgov = port["sgov"]["shares"] * quotes["sgov"]
    return {"nlv": port["cash"] + aapl + spy + sgov - opt, "cash": port["cash"],
            "aapl": aapl, "spy": spy, "sgov": sgov, "short_options": -opt}


def margin_req(port, quotes, state, now, cfg, extra_positions=()):
    """Rough portfolio-margin style requirement (additive, no offsets)."""
    m = cfg["margin"]
    spx = spx_from(state, quotes["spy"])
    vals = nlv(port, quotes, state, now)
    total = max(vals["nlv"], 1.0)
    aapl_share = vals["aapl"] / total
    aapl_pct = m["concentrated_pct"] if aapl_share > m["concentration_share"] else m["stock_pct"]
    req = vals["aapl"] * aapl_pct / 100 + vals["spy"] * m["index_etf_pct"] / 100 \
        + vals["sgov"] * m["tbill_pct"] / 100
    stressed = spx * (1 + m["index_stress_pct"] / 100)
    for p in list(port["positions"]) + list(extra_positions):
        if p["status"] != "open":
            continue
        T = px.years_to(px.expiry_dt(date.fromisoformat(p["expiry"])), now)
        v = p["entry_iv"] + m["stress_vol_pts"] / 100
        if p["kind"] == "P":
            stress_val = px.bs_put(stressed, p["strike"], T, v)
        else:
            stress_val = px.bs_call(spx * (1 - m["index_stress_pct"] / 100 * 0.75), p["strike"], T, v)
        req += stress_val * 100 * p["qty"]
    return {"requirement": req, "nlv": vals["nlv"], "excess": vals["nlv"] - req,
            "excess_pct": (vals["nlv"] - req) / total * 100}


def crash_table(port, quotes, state, cfg, extra_positions=(), now=None):
    """Same-day crash with stops failing: option intrinsic + stock moves."""
    spx = spx_from(state, quotes["spy"])
    beta = cfg["crash"]["aapl_beta"]
    base_nlv = nlv(port, quotes, state, now or datetime.now(px.ET))["nlv"]
    rows = []
    open_pos = [p for p in list(port["positions"]) + list(extra_positions) if p["status"] == "open"]
    for pct in cfg["crash"]["scenarios_pct"]:
        f = pct / 100
        s_new = spx * (1 + f)
        opt_loss = sum(max(p["strike"] - s_new, 0) * 100 * p["qty"] for p in open_pos if p["kind"] == "P")
        aapl_loss = port["aapl"]["shares"] * quotes["aapl"] * max(beta * f, -0.95)
        spy_loss = sum(l["shares"] for l in port["spy_lots"]) * quotes["spy"] * f
        sleeve = port["sgov"]["shares"] * quotes["sgov"] + max(port["cash"], 0)
        after = base_nlv - opt_loss + aapl_loss + spy_loss
        m = cfg["margin"]
        aapl_after = port["aapl"]["shares"] * quotes["aapl"] * (1 + max(beta * f, -0.95))
        req_after = aapl_after * m["concentrated_pct"] / 100 \
            + sum(l["shares"] for l in port["spy_lots"]) * quotes["spy"] * (1 + f) * m["index_etf_pct"] / 100
        rows.append({
            "spx_move_pct": pct, "option_loss": r2(opt_loss), "aapl_change": r2(aapl_loss),
            "spy_change": r2(spy_loss), "sleeve": r2(sleeve),
            "sleeve_covers_option_loss": opt_loss <= sleeve,
            "nlv_after": r2(after), "req_after": r2(req_after),
            "forced_liquidation_risk": after < req_after,
        })
    return rows


def new_portfolio(name, uses_gate, cfg, aapl_px, sgov_px, today):
    st = cfg["start"]
    basis = st["aapl_basis"] or aapl_px
    shares_sgov = math.floor(st["sleeve_usd"] / sgov_px)
    return {
        "name": name, "uses_gate": uses_gate, "cash": st["sleeve_usd"] - shares_sgov * sgov_px,
        "aapl": {"shares": st["aapl_shares"], "basis": basis, "start_shares": st["aapl_shares"]},
        "spy_lots": [], "sgov": {"shares": shares_sgov},
        "positions": [],
        "realized": {},          # year -> {"opt_1256": x, "aapl_lt": y, "spy": z, "premium": p}
        "aapl_sales": [],        # [{"date", "shares", "price", "reason"}]
        "counts": {"trades": 0, "stops": 0, "skipped_gate": 0, "skipped_rules": 0},
        "start_date": today.isoformat(),
    }


def book(port, year, key, amount):
    y = port["realized"].setdefault(str(year), {"opt_1256": 0.0, "aapl_lt": 0.0, "spy": 0.0, "premium": 0.0})
    y[key] = round(y.get(key, 0.0) + amount, 2)


# ---------------------------------------------------------------- commands

def cmd_init(a):
    cfg = load_config(a.config)
    q = load(a.quotes)
    now = parse_dt(a.now) if a.now else datetime.now(px.ET)
    state = {
        "version": 1, "created": now.isoformat(), "mode": "paper",
        "spx_spy_ratio": cfg["spx_spy_ratio"], "config": cfg,
        "portfolios": {k: new_portfolio(v["name"], v["uses_gate"], cfg, q["aapl"], q["sgov"], now.date())
                       for k, v in cfg["portfolios"].items()},
        "last_mark": None,
    }
    dump(state, a.state)
    ob = Outbox(a.outbox)
    ob.put("state", "current", state)
    hist = mark_doc(state, q, now)
    ob.put("history", now.date().isoformat(), hist)
    ob.save()
    print(json.dumps({k: r2(v["nlv"]) for k, v in hist["portfolios"].items()}))


def mark_doc(state, quotes, now):
    out = {"date": now.date().isoformat(), "asof": now.isoformat(),
           "quotes": quotes, "spx": r2(spx_from(state, quotes["spy"])), "portfolios": {}}
    for k, p in state["portfolios"].items():
        v = nlv(p, quotes, state, now)
        mr = margin_req(p, quotes, state, now, state["config"])
        out["portfolios"][k] = {kk: r2(vv) for kk, vv in v.items()}
        out["portfolios"][k].update({"margin_excess_pct": r2(mr["excess_pct"]),
                                     "aapl_shares": p["aapl"]["shares"],
                                     "realized": p["realized"]})
    state["last_mark"] = out
    return out


def _snap_spot(snap, state):
    """SPX spot: put-call parity on the snapshot if given, else SPY x ratio."""
    spy = snap["spy"]["last"]
    par = snap.get("parity")
    if par:
        mid = lambda b, a_: (b + a_) / 2
        s_par = mid(par["call_bid"], par["call_ask"]) - mid(par["put_bid"], par["put_ask"]) + par["strike"]
        spy_at = snap["spy"].get("at_quote_time") or spy
        ratio = s_par / spy_at
        if 9.8 < ratio < 10.3:
            return spy * ratio, ratio, "parity"
    return spy * state["spx_spy_ratio"], state["spx_spy_ratio"], "stored ratio"


def cmd_plan(a):
    state = load(a.state)
    cfg = state["config"]
    snap = load(a.snapshot)
    leg_name = snap["leg"]
    leg = cfg["legs"][leg_name]
    now = parse_dt(snap["asof"])
    exp_d = date.fromisoformat(snap["expiry"])
    expiry = px.expiry_dt(exp_d)
    spx, ratio, ratio_src = _snap_spot(snap, state)
    T = px.years_to(expiry, now)
    vdays = px.variance_days(now, expiry)
    hv = snap["spy"].get("hv30") or 0.15
    kind = leg["kind"]

    chain = [r for r in snap["chain"] if r.get("iv") and r["iv"] > 0]
    pts = [(r["strike"], r["iv"]) for r in chain]
    nearest = min((abs(k / spx - 1) for k, _ in pts), default=1)
    if nearest > 0.005 and snap["spy"].get("iv"):
        # the chain stops short of spot: anchor at-the-money vol with the index's
        # own implied vol so the density chart isn't flattened by OTM skew
        pts.append((spx, snap["spy"]["iv"]))
    smile = px.Smile(spx, pts)
    stop_mult = leg["stop_mult"]
    tick = cfg["costs"]["tick"]
    comm = cfg["costs"]["commission_per_contract"]
    bump = cfg["stops"]["vol_bump_pts_per_pct"]
    sd_hist = px.hist_scale(hv, vdays)

    rows = []
    for r in sorted(snap["chain"], key=lambda r: r["strike"]):
        K = r["strike"]
        iv = r.get("iv") if r.get("iv") and r["iv"] > 0 else smile.iv(K)
        otm = (spx - K) / spx * 100 if kind == "P" else (K - spx) / spx * 100
        bid = r.get("bid") or 0.0
        g = px.put_greeks(spx, K, T, iv) if kind == "P" else None
        row = {"strike": K, "otm_pct": r2(otm), "bid": bid, "ask": r.get("ask"), "iv": r4(iv),
               "contract_id_ex": r.get("contract_id_ex"),
               "model": r4(px.bs_put(spx, K, T, iv) if kind == "P" else px.bs_call(spx, K, T, iv)),
               "p_itm_rn": px.p_itm_rn(spx, K, T, iv, kind),
               "p_itm_hist": px.p_below_hist(spx, K, hv, vdays) if kind == "P"
               else 1 - px.p_below_hist(spx, K, hv, vdays)}
        if g:
            row.update({k: r4(v) for k, v in g.items()})
        if bid > 0 and kind == "P":
            stop_px = max(bid * stop_mult, leg.get("stop_min", 0), bid + 2 * tick)
            # sticky moneyness: as SPX falls the strike's IV moves along today's smile
            s_star = px.stop_spx_level(K, T, lambda S, K=K: smile.iv(K, S), stop_px, spx, bump)
            row["stop_price"] = r2(stop_px)
            row["stop_spx"] = r2(s_star)
            row["stop_drop_pct"] = r2((s_star - spx) / spx * 100)
            row["p_touch_rn"] = px.p_touch(spx, s_star, smile.iv(s_star) * math.sqrt(T))
            # fat-tail touch: 2 x t-tail beyond the stop level (reflection principle)
            row["p_touch_hist"] = min(1.0, 2 * px.p_below_hist(spx, s_star, hv, vdays))
            pt = row["p_touch_hist"]
            row["ev_hist"] = r2((1 - pt) * bid * 100 - pt * (stop_px - bid) * 100 - comm - pt * comm)
        rows.append(row)
    for row in rows:
        for k in ("p_itm_rn", "p_itm_hist", "p_touch_rn", "p_touch_hist"):
            if k in row:
                row[k] = float(f"{row[k]:.3g}")

    # strike choice: furthest OTM meeting target premium, else min premium
    cands = [r for r in rows if r["otm_pct"] >= leg["min_otm_pct"] and r["bid"] and r["contract_id_ex"]]
    chosen, why = None, None
    for floor_, label in ((leg["target_bid"], "target premium"), (leg["min_bid"], "minimum premium")):
        ok = [r for r in cands if r["bid"] >= floor_ - 1e-9]
        if ok:
            chosen = max(ok, key=lambda r: r["otm_pct"])
            why = f"furthest strike at least {leg['min_otm_pct']}% OTM with bid >= ${floor_:.2f} ({label})"
            break
    rules = []
    if chosen and chosen["strike"] == min(r["strike"] for r in rows) and kind == "P":
        rules.append("chosen strike is the furthest one fetched: extend the chain further OTM and re-plan")
    if not leg["enabled"]:
        rules.append("leg disabled in config")
    if chosen is None:
        rules.append(f"no strike {leg['min_otm_pct']}%+ OTM bids at least ${leg['min_bid']:.2f}")
    if T <= 0:
        rules.append("expiry already passed")

    # refine: ask for 5-point strikes around the chosen one if the grid was coarse
    strikes = sorted(r["strike"] for r in rows)
    refine = []
    if chosen and len(strikes) > 1:
        step = min(b - a_ for a_, b in zip(strikes, strikes[1:]))
        if step > 5:
            # the best strike lies between the pick and the next sampled strike below it
            k0 = chosen["strike"]
            sub = 5 if step <= 25 else 10
            refine = [k for k in range(int(k0 - step + sub), int(k0), sub) if k not in strikes]

    # density curves for the chart
    lo, hi = spx * 0.90, spx * 1.03
    grid = [lo + (hi - lo) * i / 160 for i in range(161)]
    dens_rn = px.rn_density(spx, smile, T, grid) if T > 0 else [0] * len(grid)
    dens_h = [px.pdf_hist(spx, x, hv, vdays) for x in grid]

    # sizing and risk per portfolio
    quotes = {"spy": snap["spy"]["last"], "aapl": snap["aapl"]["last"], "sgov": snap["sgov"]["last"]}
    state["spx_spy_ratio"] = ratio
    sizing = {}
    if chosen:
        notional = spx * 100
        for k, p in state["portfolios"].items():
            v = nlv(p, quotes, state, now)
            open_notional = sum(spx * 100 * q["qty"] for q in p["positions"] if q["status"] == "open")
            leg_open = sum(spx * 100 * q["qty"] for q in p["positions"]
                           if q["status"] == "open" and q["leg"] == leg_name)
            room = min(cfg["sizing"]["max_total_notional_x"] * v["nlv"] - open_notional,
                       leg.get("max_notional_x", 99) * v["nlv"] - leg_open)
            qty = max(int(room // notional), 0)
            trial = []
            while qty > 0:
                trial = [{"status": "open", "kind": kind, "strike": chosen["strike"], "qty": qty,
                          "expiry": exp_d.isoformat(), "entry_iv": chosen["iv"]}]
                mr = margin_req(p, quotes, state, now, cfg, trial)
                if mr["excess_pct"] >= cfg["sizing"]["min_margin_excess_pct"]:
                    break
                qty -= 1
            mr = margin_req(p, quotes, state, now, cfg, trial if qty else ())
            sizing[k] = {"qty": qty, "nlv": r2(v["nlv"]),
                         "leverage_x": r2((open_notional + qty * notional) / v["nlv"]),
                         "margin_excess_pct_after": r2(mr["excess_pct"]),
                         "crash": crash_table(p, quotes, state, cfg, trial if qty else (), now)}
            if qty == 0:
                sizing[k]["note"] = "no room under leverage cap and margin buffer"

    brief = make_brief(snap, spx, chosen, leg_name, now, rows)
    plan = {
        "plan_id": f"{now.date().isoformat()}-{leg_name}", "asof": now.isoformat(), "leg": leg_name,
        "kind": kind, "expiry": exp_d.isoformat(), "hours_left": r2(T * px.HOURS_PER_YEAR),
        "variance_days": r4(vdays), "spx": r2(spx), "spy": snap["spy"]["last"], "ratio": r4(ratio),
        "ratio_source": ratio_src, "hv30": r4(hv), "atm_iv": r4(smile.iv(spx)),
        "quote_status": snap.get("quote_status", "unknown"),
        "smile": {"k": [r4(x) for x in smile.k], "v": [r4(x) for x in smile.v]},
        "rows": rows, "chosen": chosen, "choice_rule": why, "hard_rules_failed": rules,
        "refine_strikes": refine, "sizing": sizing,
        "density": {"x": [r2(x) for x in grid], "rn": [float(f"{d:.4g}") for d in dens_rn],
                    "hist": [float(f"{d:.4g}") for d in dens_h]},
        "brief": brief, "gate": None, "decisions": {}, "fills": {}, "approval": None,
    }
    dump(plan, a.out)
    dump(state, a.state)
    if a.outbox:
        ob = Outbox(a.outbox)
        ob.put("plans", plan["plan_id"], plan)
        ob.save()
    summary = {"plan_id": plan["plan_id"], "spx": plan["spx"], "chosen": chosen and {
        k: chosen[k] for k in ("strike", "otm_pct", "bid", "iv", "p_itm_rn", "p_itm_hist",
                               "stop_price", "p_touch_hist", "ev_hist")},
        "qty": {k: v["qty"] for k, v in sizing.items()}, "rules_failed": rules, "refine": refine}
    print(json.dumps(summary, indent=1))


def _words_move(pct):
    a_ = abs(pct)
    if a_ < 0.25:
        return "about flat"
    size = "slightly" if a_ < 0.6 else "moderately" if a_ < 1.2 else "sharply" if a_ < 2.5 else "violently"
    return f"{size} {'up' if pct > 0 else 'down'} ({pct:+.1f}%)"


def _words_level(x, cuts, labels):
    for c, l in zip(cuts, labels):
        if x < c:
            return l
    return labels[-1]


def make_brief(snap, spx, chosen, leg_name, now, rows):
    s = snap["spy"]
    lines = []
    if s.get("prior_close"):
        if s.get("open"):
            lines.append(f"The S&P 500 opened {_words_move((s['open'] / s['prior_close'] - 1) * 100)} versus yesterday's close")
        lines.append(f"and is now {_words_move((s['last'] / s['prior_close'] - 1) * 100)} on the day.")
    if s.get("iv"):
        lvl = _words_level(s["iv"], [0.12, 0.17, 0.24, 0.35], ["very calm", "calm", "normal", "elevated", "panicky"])
        lines.append(f"Index implied volatility is {lvl} ({s['iv'] * 100:.0f}%).")
    if s.get("iv_pctile_52w") is not None:
        lines.append(f"That is {_words_level(s['iv_pctile_52w'], [0.2, 0.5, 0.8, 0.95], ['near the bottom of', 'in the lower half of', 'in the upper half of', 'near the top of', 'at the very top of'])} its one-year range.")
    if s.get("hv30"):
        lines.append(f"Realized volatility over the last month was {_words_level(s['hv30'], [0.10, 0.15, 0.22, 0.32], ['very calm', 'calm', 'normal', 'choppy', 'turbulent'])}.")
    when = "this afternoon to hold overnight" if leg_name.startswith("1dte") else "this morning, expiring today"
    if chosen:
        lines.append(f"The planned trade sells a put about {chosen['otm_pct']:.1f}% below the index {when}.")
    return " ".join(lines)


def _answer(item, qid):
    ans = item.get("answers", {}).get(qid, {})
    return ans.get("noul", ans.get("yes", ans.get("p")))


def cmd_gate(a):
    plan = load(a.plan)
    state = load(a.state)
    cfg = state["config"]
    g = gate_config()
    notes = []
    p_ok = None
    if a.jev:
        res = load(a.jev)
        p_ok = _answer(res[0] if isinstance(res, list) else res, "ok")
    if p_ok is None:
        notes.append("market check unavailable: treated as reject")
    hs = _headline_list(a)
    scored = []
    if hs:
        res = load(a.jev_headlines) if a.jev_headlines and os.path.exists(a.jev_headlines) else []
        by_id = {r.get("id"): _answer(r, "shock") for r in res}
        for i, h in enumerate(hs):
            sc = by_id.get(f"h{i + 1}")
            scored.append({"text": h, "shock": sc, "veto": sc is None or sc >= g["shock_veto"]})
        if any(x["shock"] is None for x in scored):
            notes.append("headline check unavailable: treated as veto")
    else:
        notes.append("no headlines were checked")
    vetoes = [x for x in scored if x["veto"]]
    market_ok = p_ok is not None and p_ok >= g["threshold"]
    plan["gate"] = {"p_ok": p_ok, "threshold": g["threshold"], "market_ok": market_ok,
                    "headline_scores": scored, "shock_veto": g["shock_veto"],
                    "max_shock": max((x["shock"] for x in scored if x["shock"] is not None), default=None),
                    "pass": market_ok and not vetoes,
                    "events": a.events, "headlines": " | ".join(hs),
                    "note": "; ".join(notes) or None}
    if not market_ok:
        why = f"gut check said no (market p={p_ok})"
    elif vetoes:
        v = max(vetoes, key=lambda x: x["shock"] if x["shock"] is not None else 2)
        why = f"headline veto: \"{v['text']}\" (shock {v['shock']})"
    else:
        why = None
    for k, p in state["portfolios"].items():
        if plan["hard_rules_failed"]:
            d = {"trade": False, "reason": "; ".join(plan["hard_rules_failed"])}
        elif plan["sizing"].get(k, {}).get("qty", 0) == 0:
            d = {"trade": False, "reason": plan["sizing"].get(k, {}).get("note", "size 0")}
        elif p["uses_gate"] and not plan["gate"]["pass"]:
            d = {"trade": False, "reason": why}
        else:
            d = {"trade": True, "reason": "rules pass" + (" and gut check passed" if p["uses_gate"] else "")}
        plan["decisions"][k] = d
    dump(plan, a.plan)
    if a.outbox:
        ob = Outbox(a.outbox)
        ob.put("plans", plan["plan_id"], plan)
        ob.save()
    want = load_config(ROOT / "config.local.json").get("approvals", {}).get("stage_ibkr", "off")
    passed = plan["gate"]["pass"] and not plan["hard_rules_failed"]
    nxt = (f"plan passed: stage_ibkr={want}. Run `stage --mode {want}`, create the IBKR instruction, "
           "then `link --instruction-id ... --ibkr-url ...`") if passed and want != "off" else \
          ("plan passed: run `link --dashboard ...` (no IBKR staging)" if passed else
           "plan did not pass: run `link` with no links; notification without click-through")
    print(json.dumps({"gate": plan["gate"], "decisions": plan["decisions"], "approval_next": nxt}, indent=1))


def cmd_fill(a):
    plan = load(a.plan)
    state = load(a.state)
    cfg = state["config"]
    ob = Outbox(a.outbox)
    now = parse_dt(plan["asof"])
    comm = cfg["costs"]["commission_per_contract"]
    c = plan["chosen"]
    leg = cfg["legs"][plan["leg"]]
    for k, p in state["portfolios"].items():
        d = plan["decisions"].get(k, {})
        if not d.get("trade"):
            key = "skipped_gate" if ("gut check" in d.get("reason", "") or "headline veto" in d.get("reason", "")) else "skipped_rules"
            p["counts"][key] += 1
            continue
        qty = plan["sizing"][k]["qty"]
        price = c["bid"]
        stop_px = c.get("stop_price") or round(max(price * leg["stop_mult"], leg.get("stop_min", 0)), 2)
        pos = {"id": f"{plan['plan_id']}-{k}", "plan_id": plan["plan_id"], "leg": plan["leg"],
               "kind": plan["kind"], "strike": c["strike"], "expiry": plan["expiry"], "qty": qty,
               "entry_price": price, "entry_time": plan["asof"], "entry_iv": c["iv"],
               "spx_entry": plan["spx"], "stop": stop_px,
               "limit": round(stop_px + leg["limit_ticks"] * cfg["costs"]["tick"], 2),
               "stop_state": "armed", "checked_until": plan["asof"], "status": "open",
               "smile": plan.get("smile")}
        p["positions"].append(pos)
        p["cash"] += qty * 100 * price - qty * comm
        book(p, now.year, "opt_1256", qty * 100 * price - qty * comm)
        book(p, now.year, "premium", qty * 100 * price)
        p["counts"]["trades"] += 1
        plan["fills"][k] = {"qty": qty, "price": price, "stop": pos["stop"], "limit": pos["limit"]}
        ob.put("events", event_id(now, k, "open", plan["leg"]),
               {"ts": now.isoformat(), "portfolio": k, "type": "open", "leg": plan["leg"],
                "strike": c["strike"], "qty": qty, "price": price,
                "cash_flow": r2(qty * 100 * price - qty * comm)})
    dump(state, a.state)
    dump(plan, a.plan)
    ob.put("plans", plan["plan_id"], plan)
    ob.put("state", "current", state, if_version=a.state_version)
    ob.save()
    print(json.dumps(plan["fills"], indent=1))


def cmd_decum(a):
    """AAPL decumulation for both portfolios (identical rules, not gated)."""
    state = load(a.state)
    cfg = state["config"]
    d = cfg["decum"]
    snap = load(a.snapshot)
    now = parse_dt(snap["asof"])
    ob = Outbox(a.outbox)
    aapl, aapl_prev = snap["aapl"]["last"], snap["aapl"].get("prior_close")
    spy, spy_prev = snap["spy"]["last"], snap["spy"].get("prior_close")
    aapl_chg = (aapl / aapl_prev - 1) * 100 if aapl_prev else 0.0
    spx_chg = (spy / spy_prev - 1) * 100 if spy_prev else 0.0
    out = {}
    for k, p in state["portfolios"].items():
        sh = p["aapl"]["shares"]
        if sh <= 0:
            out[k] = "no AAPL left"
            continue
        stopped_today = any(q.get("stopped_at", "").startswith(now.date().isoformat()) for q in p["positions"])
        reason, n = None, 0
        if spx_chg <= d["crash_spx_pct"] or stopped_today:
            reason, n = "crash day", d["crash_tranche"]
        elif aapl_chg <= d["down_day_pct"]:
            reason, n = f"AAPL down {aapl_chg:.1f}%", d["tranche"]
        elif aapl < p["aapl"]["basis"]:
            reason, n = "AAPL below stepped-up basis", d["tranche"]
        else:
            start = date.fromisoformat(p["start_date"])
            years = max((now.date() - start).days / 365.25, 0)
            sold = p["aapl"]["start_shares"] - sh
            if sold < d["min_per_year"] * years - d["tranche"]:
                reason, n = "pace backstop", d["tranche"]
        if not reason:
            out[k] = f"hold (AAPL {aapl_chg:+.1f}%)"
            continue
        if any(s["date"] == now.date().isoformat() for s in p["aapl_sales"]):
            out[k] = "already sold today"
            continue
        n = min(n, sh)
        proceeds = n * aapl
        gain = n * (aapl - p["aapl"]["basis"])
        p["aapl"]["shares"] -= n
        p["cash"] += proceeds
        book(p, now.year, "aapl_lt", gain)
        p["aapl_sales"].append({"date": now.date().isoformat(), "shares": n, "price": aapl, "reason": reason})
        ob.put("events", event_id(now, k, "aapl-sale"),
               {"ts": now.isoformat(), "portfolio": k, "type": "aapl_sale", "shares": n, "price": aapl,
                "reason": reason, "realized_lt": r2(gain), "cash_flow": r2(proceeds)})
        out[k] = f"sold {n} @ {aapl:.2f} ({reason}), LT {'gain' if gain >= 0 else 'loss'} {gain:,.0f}"
    dump(state, a.state)
    ob.put("state", "current", state, if_version=a.state_version)
    ob.save()
    print(json.dumps(out, indent=1))


def _bars(files):
    """Merge IBKR columnar bar files into [(t_end, open, high, low, close)] in ET."""
    seen = {}
    for f in files:
        b = load(f)
        step = timedelta(seconds=b.get("chart_step", 300))
        for i, t in enumerate(b["time"]):
            t0 = parse_dt(t)
            seen[t0] = (t0, t0 + step, b["open"][i], b["high"][i], b["low"][i], b["close"][i])
    return [seen[k] for k in sorted(seen)]


def _smile_iv(pos, K, S):
    sm = pos.get("smile")
    if not sm or not sm.get("k"):
        return pos["entry_iv"]
    k = math.log(K / S)
    ks, vs = sm["k"], sm["v"]
    if k <= ks[0]:
        return vs[0]
    if k >= ks[-1]:
        return vs[-1]
    for i in range(1, len(ks)):
        if k <= ks[i]:
            w = (k - ks[i - 1]) / (ks[i] - ks[i - 1])
            return vs[i - 1] * (1 - w) + vs[i] * w
    return vs[-1]


def _simulate_stop(pos, bars, ratio, bump, upto):
    """Walk bars after checked_until; returns (event or None)."""
    K, kind = pos["strike"], pos["kind"]
    start = parse_dt(pos["checked_until"])
    exp = px.expiry_dt(date.fromisoformat(pos["expiry"]))
    for t0, t1, o, h, l, c in bars:
        if t1 <= start or t0 >= min(upto, exp):
            continue
        T = px.years_to(exp, t1)

        def value(level):
            spx_l = level * ratio
            mv = (pos["spx_entry"] - spx_l) if kind == "P" else (spx_l - pos["spx_entry"])
            base = _smile_iv(pos, K, spx_l)
            v = base + bump * max(0.0, mv / pos["spx_entry"] * 100) / 100
            return px.bs_put(spx_l, K, T, v) if kind == "P" else px.bs_call(spx_l, K, T, v)

        # a bar that began before entry has no gap from the trader's point of view
        v_open = value(pos["spx_entry"] / ratio) if t0 < start else value(o)
        v_worst = value(l if kind == "P" else h)
        v_best = value(h if kind == "P" else l)
        if pos["stop_state"] == "armed":
            if v_open >= pos["stop"]:
                # the bar opened beyond the stop: a true gap
                if v_open <= pos["limit"]:
                    return {"t": t0, "price": round(v_open, 2), "how": "stop-limit filled at open"}
                pos["stop_state"] = "gapped"
                pos["gapped_at"] = t0.isoformat()
            elif v_worst >= pos["stop"]:
                # price moved through the stop inside the bar: treat as continuous,
                # the limit order fills on the way through (conservatively at the limit)
                return {"t": t1, "price": pos["limit"], "how": "stop-limit filled intrabar"}
            else:
                continue
        if pos["stop_state"] == "gapped" and v_best <= pos["limit"]:
            return {"t": t1, "price": pos["limit"], "how": "resting limit filled after gap"}
    return None


def cmd_settle(a):
    state = load(a.state)
    cfg = state["config"]
    now = parse_dt(a.now) if a.now else datetime.now(px.ET)
    q = load(a.quotes)          # {"spy":..., "aapl":..., "sgov":..., "spy_close": optional}
    if a.ratio:
        state["spx_spy_ratio"] = a.ratio
    ratio = state["spx_spy_ratio"]
    bars = _bars(a.bars or [])
    bump = cfg["stops"]["vol_bump_pts_per_pct"]
    comm = cfg["costs"]["commission_per_contract"]
    ob = Outbox(a.outbox)
    log = {}
    spx_close = (q.get("spy_close") or q["spy"]) * ratio
    for k, p in state["portfolios"].items():
        log[k] = []
        for pos in p["positions"]:
            if pos["status"] != "open":
                continue
            ev = _simulate_stop(pos, bars, ratio, bump, now)
            if ev:
                cost = pos["qty"] * 100 * ev["price"] + pos["qty"] * comm
                p["cash"] -= cost
                book(p, ev["t"].year, "opt_1256", -cost)
                pos.update({"status": "stopped", "exit_price": ev["price"], "stopped_at": ev["t"].isoformat(),
                            "pnl": r2(pos["qty"] * 100 * pos["entry_price"] - pos["qty"] * comm - cost)})
                p["counts"]["stops"] += 1
                ob.put("events", event_id(ev["t"], k, "stop", pos["leg"]),
                       {"ts": ev["t"].isoformat(), "portfolio": k, "type": "stop", "leg": pos["leg"],
                        "strike": pos["strike"], "qty": pos["qty"], "price": ev["price"], "how": ev["how"],
                        "pnl": pos["pnl"], "cash_flow": r2(-cost)})
                log[k].append(f"STOP {pos['leg']} {pos['strike']} @ {ev['price']} ({ev['how']})")
                continue
            last_bar_end = max((b[1] for b in bars), default=None)
            if last_bar_end:
                pos["checked_until"] = max(parse_dt(pos["checked_until"]), min(last_bar_end, now)).isoformat()
            exp = px.expiry_dt(date.fromisoformat(pos["expiry"]))
            if now >= exp:
                intrinsic = max(pos["strike"] - spx_close, 0) if pos["kind"] == "P" else max(spx_close - pos["strike"], 0)
                cost = pos["qty"] * 100 * intrinsic
                p["cash"] -= cost
                book(p, now.year, "opt_1256", -cost)
                pos.update({"status": "expired" if intrinsic == 0 else "assigned_cash",
                            "exit_price": round(intrinsic, 2), "settle_spx": r2(spx_close),
                            "pnl": r2(pos["qty"] * 100 * pos["entry_price"] - pos["qty"] * comm - cost)})
                ob.put("events", event_id(now, k, "expire", pos["leg"]),
                       {"ts": now.isoformat(), "portfolio": k, "type": "expire", "leg": pos["leg"],
                        "strike": pos["strike"], "qty": pos["qty"], "settle_spx": r2(spx_close),
                        "intrinsic": r2(intrinsic), "pnl": pos["pnl"], "cash_flow": r2(-cost)})
                log[k].append(f"{'expired worthless' if intrinsic == 0 else 'SETTLED ITM'} {pos['leg']} {pos['strike']} pnl {pos['pnl']}")
        # keep only open + last 30 closed positions in state
        closed = [x for x in p["positions"] if x["status"] != "open"]
        p["positions"] = [x for x in p["positions"] if x["status"] == "open"] + closed[-30:]
        log[k] += sweep(p, q, state, cfg, now, ob, k)
    hist = mark_doc(state, q, now)
    dump(state, a.state)
    ob.put("history", now.date().isoformat(), hist)
    ob.put("state", "current", state, if_version=a.state_version)
    ob.save()
    print(json.dumps({"log": log, "nlv": {k: v["nlv"] for k, v in hist["portfolios"].items()}}, indent=1))


def sweep(p, q, state, cfg, now, ob, k):
    """Cash to sleeve then SPY; negative cash from sleeve, then SPY, then AAPL (forced)."""
    msgs = []
    target = cfg["sleeve"]["target_usd"]
    sgov_val = p["sgov"]["shares"] * q["sgov"]
    if p["cash"] > 0:
        need = max(target - sgov_val, 0)
        buy = min(need, p["cash"])
        n = math.floor(buy / q["sgov"])
        if n > 0:
            p["sgov"]["shares"] += n
            p["cash"] -= n * q["sgov"]
            msgs.append(f"sleeve +{n} SGOV")
        n = math.floor(p["cash"] / q["spy"]) if p["cash"] > q["spy"] else 0
        if n > 0:
            p["spy_lots"].append({"date": now.date().isoformat(), "shares": n, "price": q["spy"]})
            p["cash"] -= n * q["spy"]
            msgs.append(f"bought {n} SPY")
    if p["cash"] < 0:
        n = min(math.ceil(-p["cash"] / q["sgov"]), p["sgov"]["shares"])
        p["sgov"]["shares"] -= n
        p["cash"] += n * q["sgov"]
        if n:
            msgs.append(f"paid {n} SGOV from sleeve")
    if p["cash"] < 0:
        while p["cash"] < 0 and p["spy_lots"]:
            lot = p["spy_lots"][-1]
            take = min(lot["shares"], math.ceil(-p["cash"] / q["spy"]))
            lot["shares"] -= take
            p["cash"] += take * q["spy"]
            book(p, now.year, "spy", take * (q["spy"] - lot["price"]))
            if lot["shares"] == 0:
                p["spy_lots"].pop()
            msgs.append(f"sold {take} SPY to cover")
    mr = margin_req(p, q, state, now, cfg)
    if p["cash"] < 0 or mr["excess"] < 0:
        short = max(-p["cash"], -mr["excess"], 0)
        n = min(math.ceil(short / q["aapl"]) + 1, p["aapl"]["shares"])
        p["aapl"]["shares"] -= n
        p["cash"] += n * q["aapl"]
        book(p, now.year, "aapl_lt", n * (q["aapl"] - p["aapl"]["basis"]))
        p["aapl_sales"].append({"date": now.date().isoformat(), "shares": n, "price": q["aapl"],
                                "reason": "FORCED (margin)"})
        ob.put("events", event_id(now, k, "forced"),
               {"ts": now.isoformat(), "portfolio": k, "type": "forced_sale", "shares": n, "price": q["aapl"]})
        msgs.append(f"FORCED: sold {n} AAPL for margin")
    return msgs


def cmd_stage(a):
    """Arguments for create_order_instruction. test mode is never marketable."""
    plan = load(a.plan)
    c = plan["chosen"]
    if not c:
        print(json.dumps({"error": "no chosen strike"}))
        return
    if a.mode == "test":
        # Harmless by construction: BUY one put at the minimum tick. If someone
        # submits it by mistake, the worst case is owning one far-OTM put (~$6).
        side, qty, limit = "BUY", 1, 0.05
    else:
        side, qty, limit = "SELL", plan["sizing"][a.portfolio]["qty"], c["bid"]
    args = {"contract_id_ex": c["contract_id_ex"], "side": side, "order_type": "LIMIT",
            "limit_price": limit, "quantity": qty, "time_in_force": "DAY"}
    out = {"instruction": args, "mode": a.mode,
           "stop_to_attach": {"type": "STP LMT", "side": "BUY", "stop": c.get("stop_price"),
                              "limit": r2((c.get("stop_price") or 0) + 3 * 0.05)},
           "alert_spx_level": c.get("stop_spx")}
    print(json.dumps(out, indent=1))


def cmd_report(a):
    state = load(a.state)
    m = state.get("last_mark") or {}
    for k, p in state["portfolios"].items():
        mk = m.get("portfolios", {}).get(k, {})
        print(f"{k} {p['name']}: NLV {mk.get('nlv')}  AAPL {p['aapl']['shares']}  trades {p['counts']}")
        for y, r in p["realized"].items():
            print(f"   {y}: {r}")


def gate_config():
    """Gate wording and thresholds come from the repo config, not the stored state."""
    return load_config(ROOT / "config.local.json")["gate"]


def _headline_list(a):
    hs = list(a.headline or [])
    if a.headlines:
        hs += [h.strip() for h in a.headlines.split(" | ")]
    return [h for h in hs if h]


def cmd_jev_input(a):
    """Write two classifier requests: the market brief (one question) and the
    headlines (each scored on its own for crash potential)."""
    plan = load(a.plan)
    g = gate_config()
    session = "afternoon" if plan["leg"].startswith("1dte") else "morning"
    text = plan["brief"]
    if a.events:
        text += " Scheduled before expiry: " + a.events.strip().rstrip(".") + "."
    market = {"ask": {"ok": {"q": g["question"].format(session=session), "yes": g["yes"], "no": g["no"]}},
              "items": [{"id": plan["plan_id"], "text": text}], "show": "json"}
    dump(market, a.out)
    hs = _headline_list(a)
    if hs:
        dump({"ask": {"shock": {"q": g["shock_question"], "yes": g["shock_yes"], "no": g["shock_no"]}},
              "items": [{"id": f"h{i + 1}", "text": h} for i, h in enumerate(hs)], "show": "json"},
             a.out_headlines)
    print(json.dumps({"market_text": text, "headlines": hs,
                      "files": [a.out] + ([a.out_headlines] if hs else [])}, indent=1))


def cmd_link(a):
    """Record the approval links (or their absence) on the plan."""
    plan = load(a.plan)
    passed = bool(plan.get("gate", {}) and plan["gate"].get("pass")) and not plan["hard_rules_failed"]
    want = load_config(ROOT / "config.local.json").get("approvals", {}).get("stage_ibkr", "off")
    staging = a.staging or want
    if passed and want != "off" and not a.instruction_id:
        sys.exit(f"config says stage_ibkr={want}: run `stage --mode {want}`, call "
                 "create_order_instruction with its instruction block, then re-run link "
                 "with --instruction-id and --ibkr-url from the result.")
    if passed and staging != want:
        sys.exit(f"--staging {staging} does not match config stage_ibkr={want}")
    plan["approval"] = {
        "status": "pending" if passed else "no-click-through",
        "gate_pass": passed, "dashboard": a.dashboard if passed else None,
        "ibkr_url": a.ibkr_url if passed else None, "instruction_id": a.instruction_id,
        "staging": staging if passed else "none", "created": datetime.now(px.ET).isoformat(),
    }
    dump(plan, a.plan)
    ob = Outbox(a.outbox)
    ob.put("plans", plan["plan_id"], plan)
    ob.save()
    print(json.dumps(plan["approval"], indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default="state.json")
    common.add_argument("--outbox", default="outbox")
    common.add_argument("--state-version", dest="state_version")

    s = sp.add_parser("init", parents=[common]); s.add_argument("--quotes", required=True)
    s.add_argument("--config"); s.add_argument("--now"); s.set_defaults(f=cmd_init)
    s = sp.add_parser("plan", parents=[common]); s.add_argument("--snapshot", required=True)
    s.add_argument("--out", default="plan.json"); s.set_defaults(f=cmd_plan)
    s = sp.add_parser("gate", parents=[common]); s.add_argument("--plan", default="plan.json")
    s.add_argument("--jev"); s.add_argument("--jev-headlines", dest="jev_headlines", default="jev_headlines.json")
    s.add_argument("--events", default=""); s.add_argument("--headlines", default="")
    s.add_argument("--headline", action="append")
    s.set_defaults(f=cmd_gate)
    s = sp.add_parser("fill", parents=[common]); s.add_argument("--plan", default="plan.json"); s.set_defaults(f=cmd_fill)
    s = sp.add_parser("decum", parents=[common]); s.add_argument("--snapshot", required=True); s.set_defaults(f=cmd_decum)
    s = sp.add_parser("settle", parents=[common]); s.add_argument("--quotes", required=True)
    s.add_argument("--bars", nargs="*"); s.add_argument("--now"); s.add_argument("--ratio", type=float)
    s.set_defaults(f=cmd_settle)
    s = sp.add_parser("stage", parents=[common]); s.add_argument("--plan", default="plan.json")
    s.add_argument("--mode", choices=["test", "live"], default="test"); s.add_argument("--portfolio", default="A")
    s.set_defaults(f=cmd_stage)
    s = sp.add_parser("report", parents=[common]); s.set_defaults(f=cmd_report)
    s = sp.add_parser("jev-input", parents=[common]); s.add_argument("--plan", default="plan.json")
    s.add_argument("--events", default=""); s.add_argument("--headlines", default="")
    s.add_argument("--headline", action="append")
    s.add_argument("--out", default="jev_in.json")
    s.add_argument("--out-headlines", dest="out_headlines", default="jev_in_headlines.json")
    s.set_defaults(f=cmd_jev_input)
    s = sp.add_parser("link", parents=[common]); s.add_argument("--plan", default="plan.json")
    s.add_argument("--dashboard"); s.add_argument("--ibkr-url", dest="ibkr_url")
    s.add_argument("--instruction-id", dest="instruction_id"); s.add_argument("--staging")
    s.set_defaults(f=cmd_link)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
