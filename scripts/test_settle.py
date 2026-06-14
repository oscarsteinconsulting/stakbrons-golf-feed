#!/usr/bin/env python3
"""Testharness som replikerar iOS BetAutoSettler-logiken (nuvarande OCH fixad)
och kör den mot riktiga slutleaderboards för att verifiera spelrättning."""

import urllib.request, json, sys

RAW = "https://raw.githubusercontent.com/oscarsteinconsulting/stakbrons-golf-feed/main/data"

def fetch(path):
    return json.load(urllib.request.urlopen(urllib.request.Request(
        f"{RAW}/{path}", headers={"User-Agent": "test", "Cache-Control": "no-cache"})))

def parse_pos(s):
    digits = ""
    for ch in s:
        if ch.isdigit(): digits += ch
        elif digits: break
    return int(digits) if digits else None

def find_player(players, sel):
    n = sel.lower()
    for p in players:
        if p["name"].lower() == n: return p
    for p in players:
        pn = p["name"].lower()
        if n in pn or pn in n: return p
    return None

# ---- NUVARANDE logik (buggig) ---------------------------------------------
def settle_current(cat, sel, players):
    cat = cat.lower()
    if find_player(players, sel) is None: return None
    p = find_player(players, sel)
    pos = parse_pos(p["position"])
    if "kval" in cat or "cut" in cat: return "won"
    if any(w in cat for w in ("vinnare","outright","winner")):
        return "won" if pos == 1 else "lost"
    topn = parse_topn(cat)
    if topn is not None:
        if pos is None: return None     # ← BUGG: pending
        return "won" if pos <= topn else "lost"
    return None

# ---- FIXAD logik ----------------------------------------------------------
def tie_rank(player, players):
    """Standard golfplacering med delade platser: 1 + antal STRIKT bättre score."""
    if player["totalValue"] >= 900: return None  # ESPN MC-markör
    better = sum(1 for q in players if q["totalValue"] < player["totalValue"] and q["totalValue"] < 900)
    return better + 1

def settle_fixed(cat, sel, players):
    cat = cat.lower()
    idx = next((i for i,p in enumerate(players) if find_one(p, sel)), None)
    if idx is None: return None
    p = players[idx]
    if "kval" in cat or "cut" in cat: return "won"
    if any(w in cat for w in ("vinnare","outright","winner")):
        return "won" if idx == 0 else "lost"     # vinnare = först i score-sorterad lista
    topn = parse_topn(cat)
    if topn is not None:
        pos = parse_pos(p["position"]) or tie_rank(p, players)
        if pos is None: return None
        return "won" if pos <= topn else "lost"
    return None

def find_one(p, sel):
    n=sel.lower(); pn=p["name"].lower()
    return pn==n or n in pn or pn in n

def parse_topn(cat):
    if "topp" not in cat and "top" not in cat: return None
    digits=""
    for ch in cat:
        if ch.isdigit(): digits+=ch
        elif digits: break
    return int(digits) if digits else None

# ---- Kör test mot RBC ------------------------------------------------------
arch = fetch("history/2026/live-rbc-canadian-open.json")
lb = arch["finalLeaderboard"]
winner = lb[0]["name"]
p5 = [p["name"] for p in lb[:5]]
p10 = [p["name"] for p in lb[:10]]
p20 = [p["name"] for p in lb[:20]]
outside = lb[30]["name"]  # spelare utanför topp 20

# Realistiska edge-spel: (kategori, spelare, förväntat facit)
tests = [
    ("📊 Vinnare (modell)",  winner,   "won"),
    ("📊 Vinnare (modell)",  p10[3],   "lost"),
    ("📊 Topp 5 (modell)",   p5[2],    "won"),
    ("📊 Topp 5 (modell)",   p10[7],   "lost"),
    ("📊 Topp 10 (modell)",  p10[8],   "won"),
    ("📊 Topp 10 (modell)",  outside,  "lost"),
    ("📊 Topp 20 (modell)",  p20[18],  "won"),
    ("📊 Topp 20 (modell)",  outside,  "lost"),
    ("📊 Klara kvalgränsen (modell)", p10[1], "won"),
]

print(f"=== RBC slutställning: vinnare={winner}, {len(lb)} spelare ===\n")
print(f"{'KATEGORI':28s} {'SPELARE':22s} {'FACIT':6s} {'NUVARANDE':12s} {'FIXAD':8s}")
cur_ok = fix_ok = 0
cur_pending = 0
for cat, sel, exp in tests:
    cur = settle_current(cat, sel, lb)
    fix = settle_fixed(cat, sel, lb)
    cm = "✓" if cur==exp else ("PENDING!" if cur is None else "FEL")
    fm = "✓" if fix==exp else "FEL"
    if cur==exp: cur_ok+=1
    if cur is None: cur_pending+=1
    if fix==exp: fix_ok+=1
    print(f"{cat:28s} {sel[:22]:22s} {exp:6s} {str(cur):6s} {cm:5s} {str(fix):6s} {fm}")

print(f"\nNUVARANDE: {cur_ok}/{len(tests)} korrekta, {cur_pending} fastnar i PENDING (rättas aldrig)")
print(f"FIXAD:     {fix_ok}/{len(tests)} korrekta")
sys.exit(0 if fix_ok==len(tests) else 1)
