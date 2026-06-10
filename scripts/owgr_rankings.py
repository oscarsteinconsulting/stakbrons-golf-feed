"""Hämtar Official World Golf Ranking (OWGR) — gratis, oberoende skill-signal.

OWGR är världens officiella golfranking, uppdaterad varje måndag. Till skillnad
från Kambi-implicit-ranking (som härleds ur Svenska Spels egna odds) är OWGR
HELT OBEROENDE av bettingmarknaden — den baseras på faktiska tävlingsresultat
de senaste två åren. Det bryter cirkulariteten i edge-modellen: nu jämför vi
en oberoende styrke-skattning mot SS:s priser, istället för SS mot sig själv.

Datakälla: apiweb.owgr.com:s publika JSON-API (samma som owgr.com:s egen
rankningstabell använder). Ingen auth, ingen nyckel.

Inte lika prediktiv som DataGolf:s strokes-gained-modell, men gratis och
genuint oberoende — vilket är det viktiga för edge-beräkningen.
"""

from __future__ import annotations

import json
import sys
import urllib.request

OWGR_API = (
    "https://apiweb.owgr.com/api/owgr/rankings/getRankings"
    "?regionId=0&pageSize={size}&pageNumber=1&sortColumn=rank&sortDirection=ASC"
)
TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) Safari/605.1.15"

# Vi behöver bara täcka spelare som realistiskt finns i ett tourfält. Topp 600
# räcker gott (även djupa fält har sällan spelare utanför topp 500 OWGR).
DEFAULT_TOP_N = 600


def normalize_name(name: str) -> str:
    """Normalisera spelarnamn — matchar edge_engine.normalize_name."""
    s = name.strip().lower()
    if "," in s:
        last, first = s.split(",", 1)
        s = f"{first.strip()} {last.strip()}"
    s = "".join(c for c in s if c.isalnum() or c.isspace())
    return " ".join(s.split())


def fetch_owgr_rankings(top_n: int = DEFAULT_TOP_N) -> dict[str, int]:
    """Hämta OWGR-rankning. Returnerar {normalized_name: rank}.

    Returnerar tom dict om API:t failar — då faller edge-modellen tillbaka
    på Kambi-implicit-ranking (med cirkularitets-varningen).
    """
    url = OWGR_API.format(size=top_n)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.load(r)
    except Exception as exc:
        print(f"  ⚠️  OWGR-rankning misslyckades: {exc}", file=sys.stderr)
        return {}

    out: dict[str, int] = {}
    for entry in data.get("rankingsList", []):
        player = entry.get("player") or {}
        name = player.get("fullName")
        rank = entry.get("rank")
        if isinstance(name, str) and isinstance(rank, int) and rank > 0:
            out[normalize_name(name)] = rank
    return out


def fetch_owgr_points(top_n: int = DEFAULT_TOP_N) -> dict[str, float]:
    """Hämta OWGR points-average (kontinuerlig skill-signal) per spelare.

    pointsAverage är mer finkornig än rank — Scheffler ~16, mid-tier ~2-4,
    botten <1. Returneras separat ifall vi vill kalibrera μ kontinuerligt
    senare. Returnerar {normalized_name: pointsAverage}.
    """
    url = OWGR_API.format(size=top_n)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.load(r)
    except Exception as exc:
        print(f"  ⚠️  OWGR-points misslyckades: {exc}", file=sys.stderr)
        return {}

    out: dict[str, float] = {}
    for entry in data.get("rankingsList", []):
        player = entry.get("player") or {}
        name = player.get("fullName")
        pts = entry.get("pointsAverage")
        if isinstance(name, str) and isinstance(pts, (int, float)):
            out[normalize_name(name)] = float(pts)
    return out


if __name__ == "__main__":
    ranks = fetch_owgr_rankings()
    print(f"Hämtade {len(ranks)} OWGR-rankningar\n")
    pts = fetch_owgr_points()
    # Visa topp 15 med både rank och points
    by_rank = sorted(ranks.items(), key=lambda x: x[1])[:15]
    print(f"  {'RANK':>4s}  {'NAMN':28s}  {'PTS-AVG':>8s}")
    for name, rank in by_rank:
        p = pts.get(name, 0)
        print(f"  {rank:>4d}  {name:28s}  {p:>8.2f}")
