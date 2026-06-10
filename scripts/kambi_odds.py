"""Hämtar Svenska Spels riktiga golf-odds från Kambis publika CDN.

Svenska Spel använder Kambi (B2B sportbook-provider) som backend för
sportbook-oddsen. Kambis offering-API är publikt och utan auth — vi hittade
det via DevTools på spela.svenskaspel.se/odds/golf.

Strukturen:
  1. listView/golf/.../competitions.json → lista av aktiva golf-events
     med top-level betOffers per event (huvudsakligen outright vinnare)
  2. betoffer/event/{eventId}.json → alla betOffers för ett event
     (Slutplacering + Bästa 5/10/20 + cut + matchups + grupper)

Odds-format: Kambi sparar decimal-odds som integer ÷ 1000 (4.50 → 4500).
Vi konverterar tillbaka vid läsning.

Marknader vi mappar mot vår edge-modell:
  - "Slutplacering"                              → market="win"
  - "Bästa 5 - inklusive delade placeringar"     → market="top5"
  - "Bästa 10 - inklusive delade placeringar"    → market="top10"
  - "Bästa 20 - inklusive delade placeringar"    → market="top20"
  - "Att klara \"cutten\""                       → market="cut"

Notera: SS prissätter inte ALLA spelare i Bästa 5/10/20/cut — bara de
mest spelade favoriterna (~10-30 spelare per marknad). Vi räknar edge%
för matchande spelare och visar bara "fair odds" för resten.

Inga TOS-bekymmer mot SS direkt — det är Kambis CDN. Kambi är OK med
passiv läsning av publika oddsar (det är så deras egen analytics-pipeline
fungerar mot tredje-parts arbitrage-scrapers redan).
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from typing import Any

KAMBI_BASE = "https://eu.offering-api.kambicdn.com/offering/v2018/svenskaspel"
KAMBI_QUERY = "channel_id=1&client_id=200&lang=sv_SE&market=SE&useCombined=true&useCombinedLive=true"
TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) Safari/605.1.15"

# VIKTIGT: Kambi använder SAMMA criterion.label ("Slutplacering") för outright
# vinnare OCH topp 5/10/20/30/40 — de skiljs bara åt via betOffer.description!
# (Detta var en bugg tidigare: alla mappades till "win" och .update() gjorde
#  att topp 5-oddsen skrev över vinnar-oddsen.)
#
# De fullfälts-markander (147 spelare) ligger under criterion "Slutplacering"
# med betOfferType "Position". description avgör vilken:
DESCRIPTION_TO_MARKET = {
    "Vinnare":  "win",
    "Topp 5":   "top5",
    "Topp 10":  "top10",
    "Topp 20":  "top20",
}

# Fallback: per-spelare Head-to-Head-marknader (~10-20 favoriter) om fullfälts-
# marknaderna saknas. Kambi-label.
H2H_LABEL_TO_MARKET = {
    "Bästa 5 - inklusive delade placeringar":     "top5",
    "Bästa 10 - inklusive delade placeringar":    "top10",
    "Bästa 20 - inklusive delade placeringar":    "top20",
}

CUT_LABEL = "Att klara \"cutten\""


def _get(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def fetch_golf_event_list() -> list[dict]:
    """Returnera lista av Kambi golf-events med minimal info.

    Varje element: {"id", "name", "tour_path", "tour_name", "start"}
    Filtrerar bort långsiktiga "majors-vinnare 2027" och liknande genom
    att kräva path > 2 nivåer (golf/tour/eventname).
    """
    url = f"{KAMBI_BASE}/listView/golf/all/all/all/competitions.json?{KAMBI_QUERY}"
    data = _get(url)
    events: list[dict] = []
    for wrapper in data.get("events", []):
        ev = wrapper.get("event", {})
        path = ev.get("path", [])
        if len(path) < 2:
            continue  # för grov för att vara en faktisk tävling
        tour_term = path[1].get("termKey") if len(path) > 1 else ""
        tour_name = path[1].get("name") if len(path) > 1 else ""
        events.append({
            "id": ev["id"],
            "name": ev.get("name", ""),
            "tour_path": [p.get("termKey", "") for p in path],
            "tour_name": tour_name,
            "tour_term": tour_term,
            "start": ev.get("start"),
            "state": ev.get("state"),
        })
    return events


def fetch_event_offers(event_id: int) -> list[dict]:
    """Hämta ALLA betOffers för ett event (alla marknader)."""
    url = f"{KAMBI_BASE}/betoffer/event/{event_id}.json?{KAMBI_QUERY}"
    data = _get(url)
    return data.get("betOffers", [])


def _decode_odds(raw: int | float | None) -> float | None:
    """Kambi sparar decimal-odds som int × 1000 (4500 = 4.50)."""
    if raw is None:
        return None
    try:
        return float(raw) / 1000.0
    except (TypeError, ValueError):
        return None


def _extract_outright(offer: dict) -> dict[str, float]:
    """För Slutplacering: outcomes är alla spelare med vinnar-odds.

    Returnerar {normalized_name: decimal_odds}.
    """
    out: dict[str, float] = {}
    for o in offer.get("outcomes", []):
        name = o.get("participant") or o.get("label")
        odds = _decode_odds(o.get("odds"))
        if name and odds and o.get("status", "OPEN") == "OPEN":
            out[normalize_name(name)] = odds
    return out


def _extract_yes_per_player(offer: dict) -> dict[str, float]:
    """För Bästa 5/10/20 och cut: outcomes är 1-2 stycken,
    "Ja"-outcomen har spelarnamnet i `participant`.

    En spelare per betOffer, så vi returnerar {name: yes_odds}.
    """
    out: dict[str, float] = {}
    yes_outcome = None
    for o in offer.get("outcomes", []):
        # OT_YES eller label="Ja" eller untyped med participant
        otype = o.get("type", "")
        if otype == "OT_YES" or o.get("label") == "Ja":
            yes_outcome = o
            break
    # Fallback: om vi inte hittade "Ja" men det finns en participant — ta första
    if yes_outcome is None:
        for o in offer.get("outcomes", []):
            if o.get("participant"):
                yes_outcome = o
                break
    if yes_outcome:
        name = yes_outcome.get("participant") or yes_outcome.get("label")
        odds = _decode_odds(yes_outcome.get("odds"))
        if name and odds and yes_outcome.get("status", "OPEN") == "OPEN":
            out[normalize_name(name)] = odds
    return out


def extract_markets(offers: list[dict]) -> dict[str, dict[str, float]]:
    """Plocka ut alla intressanta marknader från en event:s betOffers.

    Primärkälla: fullfälts-marknaderna under criterion "Slutplacering",
    diskriminerade via betOffer.description ("Vinnare"/"Topp 5"/"Topp 10"/
    "Topp 20"). Dessa prissätter ALLA spelare i fältet (147 för PGA).

    Fallback per marknad: per-spelare Head-to-Head ("Bästa N") om fullfälts-
    marknaden saknas helt.

    Returnerar:
      {
        "win":   {"tommy fleetwood": 12.0, "aaron rai": 34.0, ...},  # outright
        "top5":  {"tommy fleetwood": 3.5, ...},
        "top10": {...}, "top20": {...}, "cut": {...}
      }
    """
    markets: dict[str, dict[str, float]] = {
        "win": {}, "top5": {}, "top10": {}, "top20": {}, "cut": {}
    }

    # Pass 1: fullfälts-marknader via description
    for bo in offers:
        if bo.get("closed") is True:
            continue
        crit = bo.get("criterion") or {}
        if crit.get("label") != "Slutplacering":
            continue
        description = (bo.get("description") or "").strip()
        market = DESCRIPTION_TO_MARKET.get(description)
        if not market:
            continue  # Topp 30/40 etc — vi modellerar dem inte
        # Fullfälts-marknad → outright-style extraction (alla spelare)
        odds_map = _extract_outright(bo)
        if odds_map:
            markets[market] = odds_map  # ersätt, inte merge — varje desc är unik

    # Pass 2: H2H-fallback för topp-marknader som saknar fullfälts-data
    for bo in offers:
        if bo.get("closed") is True:
            continue
        label = (bo.get("criterion") or {}).get("label")
        market = H2H_LABEL_TO_MARKET.get(label)
        if market and not markets[market]:
            # bara om fullfälts-marknaden saknades
            markets[market].update(_extract_yes_per_player(bo))

    # Pass 3: cut-marknad (Head-to-Head Ja/Nej per spelare)
    for bo in offers:
        if bo.get("closed") is True:
            continue
        label = (bo.get("criterion") or {}).get("label")
        if label == CUT_LABEL:
            markets["cut"].update(_extract_yes_per_player(bo))

    return markets


# ---------------------------------------------------------------------------
# Namn-normalisering — matchar edge_engine.normalize_name
# ---------------------------------------------------------------------------


def normalize_name(name: str) -> str:
    """Normalisera spelarnamn — matchar edge_engine.normalize_name."""
    s = name.strip().lower()
    if "," in s:
        last, first = s.split(",", 1)
        s = f"{first.strip()} {last.strip()}"
    s = "".join(c for c in s if c.isalnum() or c.isspace())
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# Event-matching mot vår slugify
# ---------------------------------------------------------------------------


def event_slug(name: str) -> str:
    """Stripa "2026" och "presented by"-suffix och slugify för matching
    mot ESPN-namn."""
    s = name.lower()
    # Ta bort årstal i slutet ("RBC Canadian Open 2026" → "rbc canadian open")
    for year in ("2025", "2026", "2027", "2028"):
        if s.endswith(year):
            s = s[: -len(year)].strip()
            break
    if " pres. by " in s:
        s = s.split(" pres. by ")[0]
    if "presented by" in s:
        s = s.split("presented by")[0].strip()
    if s.startswith("the "):
        s = s[4:]
    cleaned = []
    last_dash = True
    for c in s:
        if c.isalnum():
            cleaned.append(c)
            last_dash = False
        elif not last_dash:
            cleaned.append("-")
            last_dash = True
    return "".join(cleaned).strip("-")


# ---------------------------------------------------------------------------
# Top-level pipeline
# ---------------------------------------------------------------------------


def fetch_all_markets() -> dict[str, dict[str, Any]]:
    """Hämta alla marknader för alla aktiva golf-events.

    Returnerar:
      {
        "rbc-canadian-open": {
            "kambi_id": 1027936933,
            "name": "RBC Canadian Open 2026",
            "tour": "PGA-touren",
            "markets": {"win": {...}, "top5": {...}, "top10": {...}, "top20": {...}, "cut": {...}},
        },
        ...
      }
    """
    try:
        events = fetch_golf_event_list()
    except Exception as exc:
        print(f"  ⚠️  Kambi event-lista failade: {exc}", file=sys.stderr)
        return {}

    out: dict[str, dict[str, Any]] = {}
    for ev in events:
        try:
            offers = fetch_event_offers(ev["id"])
        except Exception as exc:
            print(f"  ⚠️  Kambi offers failade för event {ev['id']} ({ev['name']}): {exc}",
                  file=sys.stderr)
            continue
        markets = extract_markets(offers)
        # Hoppa events utan någon vinnar-marknad (long-shot futures eller dead listings)
        if not markets["win"]:
            continue
        slug = event_slug(ev["name"])
        out[slug] = {
            "kambi_id": ev["id"],
            "name": ev["name"],
            "tour": ev["tour_name"],
            "markets": markets,
            "stats": {
                "win": len(markets["win"]),
                "top5": len(markets["top5"]),
                "top10": len(markets["top10"]),
                "top20": len(markets["top20"]),
                "cut": len(markets["cut"]),
            },
        }
    return out


if __name__ == "__main__":
    # Smoke test
    data = fetch_all_markets()
    print(f"Hämtade {len(data)} golf-events från Kambi:\n")
    for slug, info in data.items():
        s = info["stats"]
        print(f"  {slug:40s}  win={s['win']:3d}  T5={s['top5']:2d}  "
              f"T10={s['top10']:2d}  T20={s['top20']:2d}  cut={s['cut']:2d}  "
              f"({info['tour']})")
