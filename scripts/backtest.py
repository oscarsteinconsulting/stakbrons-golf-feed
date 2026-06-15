#!/usr/bin/env python3
"""Backtest: jämför edge-modellens FÖRUTSÄGELSER mot FAKTISKA utfall för
avslutade tävlingar. Mäter kalibrering (säger modellen 30% topp-10 → händer
det ~30% av gångerna?) och diskriminering, för att avgöra om modellens
sannolikheter — och därmed dess edge-signaler — håller.

Använder den RIKTIGA edge_engine-koden (ingen reimplementering) så att vad vi
mäter är exakt vad appen visar.
"""
import sys, urllib.request, json, math
sys.path.insert(0, ".")
import edge_engine as E
import owgr_rankings as O

RAW = "https://raw.githubusercontent.com/oscarsteinconsulting/stakbrons-golf-feed/main/data"
N_SIMS = 20000  # högre för stabil kalibrering

def fetch(path):
    return json.load(urllib.request.urlopen(urllib.request.Request(
        f"{RAW}/{path}", headers={"User-Agent": "bt", "Cache-Control": "no-cache"})))

def actual_positions(lb):
    """{normaliserat_namn: faktisk_placering} härlett från score (delade platser)."""
    out = {}
    for p in lb:
        tv = p["totalValue"]
        if tv >= 900: continue
        better = sum(1 for q in lb if q["totalValue"] < tv and q["totalValue"] < 900)
        out[E.normalize_name(p["name"])] = better + 1
    return out

def predict(field_names, owgr_pts, n_field):
    """Kör modellens MC pre-tournament (μ från OWGR) → predikterade sannolikheter."""
    players = []
    for nm in field_names:
        norm = E.normalize_name(nm)
        mu = E.baseline_mu(norm, {}, owgr_pts)   # OWGR-points-baserad μ
        players.append({"name": nm, "mu": mu, "completed_score": 0, "missed_cut": False})
    return E.simulate_field(players, remaining_rounds=4, n_sims=N_SIMS, seed=7)

def main():
    owgr_pts = O.fetch_owgr_points()
    idx = fetch("history/index.json")
    # Bara 72-håls 4-rond-event (ej LIV som är 54 hål utan cut)
    events = [e for e in idx["entries"] if (e.get("tour") or "") != "LIV Golf"]
    print(f"Backtestar {len(events)} avslutade tävlingar (OWGR {len(owgr_pts)} spelare, {N_SIMS} sims)\n")

    # Samla (predikterad_prob, träff 0/1) per marknad
    pairs = {"win": [], "top5": [], "top10": [], "top20": []}
    TOPN = {"win": 1, "top5": 5, "top10": 10, "top20": 20}
    per_event = []

    for e in events:
        arch = fetch(e["path"])
        lb = arch["finalLeaderboard"]
        names = [p["name"] for p in lb if p["totalValue"] < 900]
        if len(names) < 20: continue
        actual = actual_positions(lb)
        probs = predict(names, owgr_pts, len(names))
        # toppspelare per modell (för enkel sammanfattning)
        ranked = sorted(names, key=lambda n: -probs.get(n, {}).get("win", 0))
        winner = min(actual, key=actual.get)
        pred_fav = E.normalize_name(ranked[0])
        per_event.append((e["name"], ranked[0], winner_name(lb, winner), probs.get(ranked[0],{}).get("win",0)))
        for nm in names:
            norm = E.normalize_name(nm)
            pos = actual.get(norm)
            if pos is None: continue
            pr = probs.get(nm, {})
            for mk, n in TOPN.items():
                p = pr.get(mk, 0.0)
                hit = 1 if pos <= n else 0
                pairs[mk].append((p, hit))

    print("=== Per tävling: modellens förhandsfavorit vs faktisk vinnare ===")
    for name, fav, win, p in per_event:
        flag = "✓" if E.normalize_name(fav)==E.normalize_name(win) else " "
        print(f"  {name[:30]:30s} fav={fav[:18]:18s} ({p*100:4.1f}%)  vinnare={win[:18]:18s} {flag}")

    print("\n=== Kalibrering per marknad (predikterad vs faktisk träff%) ===")
    for mk in ("win","top5","top10","top20"):
        ps = pairs[mk]
        if not ps: continue
        brier = sum((p-h)**2 for p,h in ps)/len(ps)
        mean_pred = sum(p for p,_ in ps)/len(ps)
        mean_act  = sum(h for _,h in ps)/len(ps)
        # binned kalibrering
        print(f"\n  {mk.upper()}  (n={len(ps)}, Brier={brier:.4f}, snitt pred={mean_pred*100:.1f}% vs faktisk={mean_act*100:.1f}%)")
        bins = [(0,.05),(.05,.10),(.10,.20),(.20,.35),(.35,.60),(.60,1.01)]
        for lo,hi in bins:
            grp = [(p,h) for p,h in ps if lo<=p<hi]
            if not grp: continue
            mp = sum(p for p,_ in grp)/len(grp)
            ma = sum(h for _,h in grp)/len(grp)
            bias = ma-mp
            bar = "över" if mp>ma+0.03 else ("under" if mp<ma-0.03 else "ok")
            print(f"    pred {lo*100:3.0f}-{hi*100:3.0f}%  n={len(grp):3d}  snitt-pred={mp*100:5.1f}%  faktisk={ma*100:5.1f}%  ({bar})")

def winner_name(lb, norm):
    for p in lb:
        if E.normalize_name(p["name"])==norm: return p["name"]
    return norm

if __name__ == "__main__":
    main()
