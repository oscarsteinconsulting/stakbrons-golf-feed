"""Edge-engine för Stakbrons Golf Odds.

Producerar fair odds + uppskattat Svenska Spel-pris för marknaderna:
- Vinnare (outright)
- Topp 5
- Topp 10
- Topp 20

via full field Monte Carlo. Spelar-skill (μ vs par per rond) approximeras
från en kombination av:

  1. DataGolf world rank (kräver DATAGOLF_API_KEY env var, gratis tier OK)
     → baseline μ per ranking-kvartil
  2. Score-to-par i pågående tävling från ESPN (om tävlingen är igång)
     → överskriver baseline med faktisk form denna vecka

Variansen sätts till σ = 2.8 slag/rond — empirisk siffra för PGA Tour-rondana.
Modellen är medvetet enkel: det är en MVP för att se om det är värt att
bygga upp en riktig SG-databas senare.

UTAN riktiga Svenska Spel-odds kan vi INTE räkna riktig edge. Vi visar
istället:
  - Fair odds från vår modell
  - Uppskattat SS-pris (fair_odds × 0.91 ≈ 10% vig)
  - Confidence-bucket (Stark / Lutar / Jämn / Svag)

Användaren får själv jämföra med riktiga SS-priser i deras app.
"""

from __future__ import annotations

import json
import os
import random
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Konstanter
# ---------------------------------------------------------------------------

ROUND_STD_DEV = 2.8  # σ per rond — empiriskt för PGA Tour
N_SIMULATIONS = 10_000
ASSUMED_SS_VIG = 0.10  # ~10% bookmaker margin på golf
RANDOM_SEED = 42  # deterministiskt mellan körningar samma dag

# Baseline μ per rond vs par baserat på världsranking (DG eller OWGR proxy).
# Spreaden är medvetet skarp så att Monte Carlo (σ=2.8/rond, 4 rondan) faktiskt
# differentierar topp-spelare från journeymen. Empiriskt ger detta Scheffler-typ
# spelare ~15-18% pre-tournament-vinst i ett 150-fält, vilket matchar marknadens
# vanliga prissättning.
RANK_TO_MU = [
    (10,    -2.00),  # top 10: dominerande nivå (Scheffler, Rahm, McIlroy)
    (25,    -1.50),  # top 25: regelbundna fönsterspelare
    (50,    -0.90),  # top 50: konsistenta toppspelare
    (100,   -0.40),  # top 100: solid PGA Tour
    (175,    0.20),  # top 175: lite under fält-snitt
    (300,    1.00),  # top 300: tydligt under
    (500,    2.00),  # top 500: minor tour nivå
    (10_000, 3.00),  # unranked / amatörer / DP World mid-tier: 3 över snitt
]


def mu_from_rank(rank: int | None) -> float:
    """Returnera baseline-μ från world rank."""
    if rank is None or rank <= 0:
        return 1.50  # ingen rank → behandla som svagt klassificerad
    for max_rank, mu in RANK_TO_MU:
        if rank <= max_rank:
            return mu
    return 2.00


# ---------------------------------------------------------------------------
# DataGolf gratis-endpoint för rankings
# ---------------------------------------------------------------------------

DATAGOLF_BASE = "https://feeds.datagolf.com"


def fetch_dg_rankings(api_key: str | None) -> dict[str, int]:
    """Hämta DataGolf:s ranking-lista. Returnerar {normalized_name: rank}.

    Kräver gratis API-nyckel från datagolf.com. Returnerar tom dict om
    nyckeln saknas eller om endpointen failar — då faller vi tillbaka på
    rank=None för alla spelare (behandlas som unranked).
    """
    if not api_key:
        return {}
    url = f"{DATAGOLF_BASE}/preds/get-dg-rankings?key={urllib.parse.quote(api_key)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        print(f"  ⚠️  DataGolf rankings unavailable: {exc}")
        return {}
    except Exception as exc:
        print(f"  ⚠️  DataGolf rankings parse error: {exc}")
        return {}

    rankings: dict[str, int] = {}
    # DG returnerar typiskt {"rankings": [{"player_name": "...", "dg_skill_rank": N, ...}]}
    items = data.get("rankings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    for item in items:
        name = item.get("player_name") or item.get("name")
        rank = item.get("dg_skill_rank") or item.get("rank") or item.get("dg_rank")
        if isinstance(name, str) and isinstance(rank, int) and rank > 0:
            rankings[normalize_name(name)] = rank
    return rankings


def normalize_name(name: str) -> str:
    """Normalisera spelarnamn för matchning mellan källor.

    DG: "Scheffler, Scottie"  → "scottie scheffler"
    ESPN: "Scottie Scheffler"  → "scottie scheffler"
    """
    s = name.strip().lower()
    if "," in s:
        last, first = s.split(",", 1)
        s = f"{first.strip()} {last.strip()}"
    # Ta bort interpunktion (apostrofer i "O'Hair" etc.)
    s = "".join(c for c in s if c.isalnum() or c.isspace())
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# Skill-estimation
# ---------------------------------------------------------------------------


def implicit_rank_from_kambi(kambi_win_odds: dict[str, float]) -> dict[str, int]:
    """Sortera spelare på Kambis vinst-odds → implicit ranking-position.

    Lägst odds = mest favoriserad = rank 1. Detta funkar utmärkt som proxy
    för world ranking eftersom SS:s odds redan kondensar bookmaker-konsensusen
    om varje spelares form, course-fit, kondition och history.

    Returnerar {normalized_name: implicit_rank}.
    """
    sorted_pairs = sorted(kambi_win_odds.items(), key=lambda x: x[1])
    return {name: i + 1 for i, (name, _) in enumerate(sorted_pairs)}


def estimate_player_mu(
    *,
    name: str,
    rankings: dict[str, int],
    completed_rounds: int,
    score_to_par_so_far: int | None,
) -> tuple[float, str]:
    """Returnerar (μ, källa) för en spelare.

    Strategi:
      - Om vi har ≥1 spelad rond denna vecka → score_to_par / completed_rounds
        (rå current form, perfekt signal denna vecka)
      - Annars använd ranking-baseline (DG-rank eller Kambi-implicit-rank)
    """
    if completed_rounds >= 1 and score_to_par_so_far is not None:
        mu_current = score_to_par_so_far / completed_rounds
        # Blend mot ranking baseline för spelare med få rondan (regression to mean)
        normalized = normalize_name(name)
        rank = rankings.get(normalized)
        mu_baseline = mu_from_rank(rank)
        if completed_rounds == 1:
            mu = 0.6 * mu_current + 0.4 * mu_baseline
            source = f"r1 form blended w/ rank {rank or '?'}"
        elif completed_rounds == 2:
            mu = 0.75 * mu_current + 0.25 * mu_baseline
            source = f"r1+r2 form blended w/ rank {rank or '?'}"
        else:
            mu = mu_current
            source = f"{completed_rounds}r form i tävlingen"
        return mu, source

    # Pre-tournament / R1 ej startad → rent ranking-baserat
    normalized = normalize_name(name)
    rank = rankings.get(normalized)
    return mu_from_rank(rank), f"rank {rank or 'unranked'}"


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


def gaussian(rng: random.Random, mu: float, sigma: float) -> float:
    """Standard Box-Muller via random.gauss för portability."""
    return rng.gauss(mu, sigma)


def simulate_field(
    players: list[dict[str, Any]],
    *,
    remaining_rounds: int,
    n_sims: int = N_SIMULATIONS,
    sigma: float = ROUND_STD_DEV,
    seed: int = RANDOM_SEED,
) -> dict[str, dict[str, float]]:
    """Kör Monte Carlo över hela fältet.

    `players`: lista av dicts med nycklar:
        - "name": str
        - "mu": float  (förväntad score vs par per rond)
        - "completed_score": int  (score-to-par hittills, 0 om pre-tournament)
        - "made_cut": bool  (om vi är efter R2 — uteslut MC-failade spelare)

    `remaining_rounds`: antal ronder kvar att simulera (4 pre-tournament,
        3 efter R1, 2 efter R2, 1 efter R3, 0 efter R4).

    Returnerar dict {player_name: {"win": p, "top5": p, "top10": p, "top20": p}}
    """
    rng = random.Random(seed)
    active = [p for p in players if not p.get("missed_cut", False)]
    n = len(active)
    if n == 0 or remaining_rounds <= 0:
        return {}

    counts: dict[str, dict[str, int]] = {
        p["name"]: {"win": 0, "top5": 0, "top10": 0, "top20": 0} for p in active
    }

    for _ in range(n_sims):
        totals: list[tuple[float, str]] = []
        for p in active:
            score = p.get("completed_score", 0) or 0
            for _ in range(remaining_rounds):
                score += gaussian(rng, p["mu"], sigma)
            totals.append((score, p["name"]))

        totals.sort(key=lambda t: t[0])

        for idx, (_, name) in enumerate(totals):
            position = idx + 1  # 1-indexed
            if position == 1:
                counts[name]["win"] += 1
            if position <= 5:
                counts[name]["top5"] += 1
            if position <= 10:
                counts[name]["top10"] += 1
            if position <= 20:
                counts[name]["top20"] += 1

    return {
        name: {market: c / n_sims for market, c in markets.items()}
        for name, markets in counts.items()
    }


# ---------------------------------------------------------------------------
# Edge / pricing
# ---------------------------------------------------------------------------


def fair_odds(prob: float) -> float | None:
    """Returnera 1/p, rundad till 2 decimaler. None om p ≤ 0 eller p ≥ 1."""
    if prob <= 0 or prob >= 1.0:
        return None
    return round(1.0 / prob, 2)


def estimated_ss_odds(fair: float | None, vig: float = ASSUMED_SS_VIG) -> float | None:
    """Uppskatta vad Svenska Spel typiskt sätter givet fair odds.

    SS har ~10% margin på golf → priset blir ungefär fair × (1 - vig).
    """
    if fair is None:
        return None
    return round(fair * (1 - vig), 2)


def confidence_bucket(prob: float, market: str) -> str:
    """Kategorisera självsäkerheten i ett tip baserat på modellprob.

    Används som fallback när vi saknar riktiga Kambi-odds.
    Trösklar är marknadsspecifika eftersom basisrate skiljer:
      - vinst: bas 1/156 ≈ 0.6% → 5%+ är stark favorit
      - topp 5: bas 3.2% → 12%+ är stark
      - topp 10: bas 6.4% → 22%+ är stark
      - topp 20: bas 12.8% → 38%+ är stark
    """
    thresholds = {
        "win":   (0.10, 0.05, 0.025),
        "top5":  (0.22, 0.14, 0.07),
        "top10": (0.38, 0.25, 0.12),
        "top20": (0.55, 0.40, 0.20),
    }
    high, mid, low = thresholds.get(market, (0.30, 0.15, 0.07))
    if prob >= high:
        return "Stark"
    if prob >= mid:
        return "Lutar"
    if prob >= low:
        return "Jämn"
    return "Svag"


def edge_confidence(edge_pct: float) -> str:
    """Kategorisera självsäkerheten i en pick baserat på RIKTIG edge%.

    Namnen är skrivna för slutanvändaren — direkta råd, inte tekniska termer.
    """
    if edge_pct >= 0.15:
        return "Spelvärt"
    if edge_pct >= 0.07:
        return "Bra läge"
    if edge_pct >= 0.03:
        return "Neutral"
    if edge_pct >= 0:
        return "Chansning"
    return "Övervärderad"


def compute_edge(prob: float, real_odds: float | None) -> dict[str, Any] | None:
    """Räkna edge% + kvart-Kelly stake för en pick mot Kambi-odds.

    Returnerar None om real_odds saknas eller är ogiltigt.

    Kelly formula: f* = (p × odds - 1) / (odds - 1)
    Vi rekommenderar kvart-Kelly för säkerhet mot modellosäkerhet.
    """
    if real_odds is None or real_odds <= 1.0:
        return None
    edge_pct = prob * real_odds - 1.0
    if real_odds <= 1.0:
        return None
    kelly_full = edge_pct / (real_odds - 1)
    # Kvart-Kelly, klamp till [0, 0.05] — aldrig mer än 5% av bankrullen per vad
    kelly_quarter = max(0.0, min(0.05, kelly_full / 4.0))
    return {
        "edgePct": round(edge_pct, 4),
        "kellyFraction": round(kelly_quarter, 4),
        "recommendedStakePct": round(kelly_quarter * 100, 2),
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def build_edge_payload(
    *,
    tournament_name: str,
    tour: str,
    completed_rounds: int,
    field: list[dict[str, Any]],
    rankings: dict[str, int],
    kambi_markets: dict[str, dict[str, float]] | None = None,
    n_sims: int = N_SIMULATIONS,
) -> dict[str, Any] | None:
    """Producera komplett edge-payload för en tävling.

    `field` är lista av dicts från ESPN-pipelinen med nycklar:
        - "name": str
        - "score_to_par": int | None  (None = ej startat / MC)
        - "made_cut": bool | None  (None = inte ännu avgjort)
    """
    remaining = 4 - completed_rounds
    if remaining <= 0:
        return None  # tävlingen är slut → ingen edge att räkna
    if not field:
        return None

    # Slå ihop DG-rankings med Kambi-implicit-rankings.
    # DG-rank har prioritet (mer noggrann), Kambi fyller i de spelare som
    # DG inte har täckning för. Det betyder att SS:s eget urval av "vilka
    # spelare som är värda odds" påverkar vår modells μ — vilket är bra
    # eftersom SS redan har bakat in form, kondition, course-fit i sin lista.
    merged_rankings: dict[str, int] = {}
    rank_source = "saknas"
    if kambi_markets and kambi_markets.get("win"):
        kambi_ranks = implicit_rank_from_kambi(kambi_markets["win"])
        merged_rankings.update(kambi_ranks)
        rank_source = f"Kambi-implicit ({len(kambi_ranks)} spelare)"
    if rankings:
        merged_rankings.update(rankings)  # DG overrider Kambi när tillgänglig
        rank_source = f"DG ({len(rankings)} sp) + Kambi-fallback"

    # Skydd mot meningslös output: utan något att basera μ på
    # blir alla spelares μ identiska (default 1.5) och Monte Carlo blir ren slump.
    has_useful_data = bool(merged_rankings) or completed_rounds >= 1
    if not has_useful_data:
        return None

    players: list[dict[str, Any]] = []
    for raw in field:
        name = raw.get("name")
        if not name:
            continue
        score = raw.get("score_to_par")
        missed_cut = bool(raw.get("missed_cut", False))
        mu, source = estimate_player_mu(
            name=name,
            rankings=merged_rankings,
            completed_rounds=completed_rounds if not missed_cut else 0,
            score_to_par_so_far=score if not missed_cut else None,
        )
        players.append({
            "name": name,
            "mu": mu,
            "mu_source": source,
            "completed_score": score if score is not None else 0,
            "missed_cut": missed_cut,
        })

    probs = simulate_field(
        players,
        remaining_rounds=remaining,
        n_sims=n_sims,
    )

    picks: list[dict[str, Any]] = []
    for p in players:
        if p["missed_cut"]:
            continue
        market_probs = probs.get(p["name"])
        if not market_probs:
            continue
        nname = normalize_name(p["name"])
        entry: dict[str, Any] = {
            "name": p["name"],
            "mu": round(p["mu"], 2),
            "muSource": p["mu_source"],
            "completedScore": p["completed_score"],
            "markets": {},
        }
        for market, prob in market_probs.items():
            fair = fair_odds(prob)
            real_odds = None
            if kambi_markets:
                real_odds = (kambi_markets.get(market) or {}).get(nname)
            edge_data = compute_edge(prob, real_odds)
            mk: dict[str, Any] = {
                "prob": round(prob, 4),
                "fairOdds": fair,
            }
            if real_odds is not None:
                mk["realSSOdds"] = round(real_odds, 2)
            else:
                mk["estimatedSSOdds"] = estimated_ss_odds(fair)
            if edge_data:
                mk.update(edge_data)
                mk["confidence"] = edge_confidence(edge_data["edgePct"])
            else:
                mk["confidence"] = confidence_bucket(prob, market)
            entry["markets"][market] = mk
        picks.append(entry)

    # Sortera primärt på vinst-prob (för backward-kompat med UI). UI:t sorterar
    # själv på edge% när det vill visa "bästa edge"-listan.
    picks.sort(key=lambda x: -(x["markets"].get("win", {}).get("prob", 0) or 0))

    payload: dict[str, Any] = {
        "modelVersion": "0.3.0",  # korrekt marknadsmappning + longshot-filter
        "simulations": n_sims,
        "remainingRounds": remaining,
        "completedRounds": completed_rounds,
        "sigmaPerRound": ROUND_STD_DEV,
        "assumedSSVig": ASSUMED_SS_VIG,
        "fieldSize": len([p for p in players if not p["missed_cut"]]),
        "hasRealOdds": bool(kambi_markets),
        "picks": picks,
        "topByMarket": _top_picks_by_market(picks, k=8),
    }
    if kambi_markets:
        # Bonus: en sorterad lista över bästa edges över alla marknader
        payload["topEdges"] = _top_edges(picks, k=10)
    return payload


def _top_picks_by_market(picks: list[dict[str, Any]], k: int) -> dict[str, list[dict[str, Any]]]:
    """Returnera top-k spelare per marknad — för enkel UI-rendering.

    Prioriterar edge% när det finns (riktiga odds), annars bara modellprob.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for market in ("win", "top5", "top10", "top20"):
        # Sortera per-marknadsvyn på modellsannolikhet (favoriter först).
        # Edge visas som annotation per rad. Detta håller longshot-brus borta
        # från flik-vyn — "Bästa edges"-listan (topEdges) är den edge-sorterade.
        def sort_key(p, _m=market):
            mk = p["markets"].get(_m, {})
            return -(mk.get("prob") or 0)

        ranked = sorted(picks, key=sort_key)
        # Filtrera till spelare som har minst prob > 0
        entries = []
        for p in ranked:
            mk = p["markets"].get(market, {})
            if (mk.get("prob") or 0) <= 0:
                continue
            entry = {
                "name": p["name"],
                "prob": mk["prob"],
                "fairOdds": mk.get("fairOdds"),
                "confidence": mk["confidence"],
            }
            if "realSSOdds" in mk:
                entry["realSSOdds"] = mk["realSSOdds"]
            if "estimatedSSOdds" in mk:
                entry["estimatedSSOdds"] = mk["estimatedSSOdds"]
            if "edgePct" in mk:
                entry["edgePct"] = mk["edgePct"]
                entry["kellyFraction"] = mk["kellyFraction"]
                entry["recommendedStakePct"] = mk["recommendedStakePct"]
            entries.append(entry)
            if len(entries) >= k:
                break
        out[market] = entries
    return out


# Lägsta modellsannolikhet för att en edge ska räknas som trovärdig.
# Under detta domineras "edge%" av modellbrus (longshots där vi inte kan
# skilja 0.4% från 0.8% men oddsen är enorma). Filtrerar bort dem.
MIN_CREDIBLE_PROB = 0.04

# Högsta odds vi litar på. Över detta är prissättningen så gles att vår
# normalfördelnings-approximation inte är meningsfull.
MAX_CREDIBLE_ODDS = 51.0


def _is_credible_edge(mk: dict[str, Any]) -> bool:
    """En edge är trovärdig om modellen har signal: tillräckligt hög
    sannolikhet OCH inte ett extremt longshot-odds."""
    prob = mk.get("prob") or 0
    odds = mk.get("realSSOdds") or 0
    edge = mk.get("edgePct")
    if edge is None or edge <= 0:
        return False
    if prob < MIN_CREDIBLE_PROB:
        return False
    if odds > MAX_CREDIBLE_ODDS:
        return False
    return True


def _top_edges(picks: list[dict[str, Any]], k: int) -> list[dict[str, Any]]:
    """Sammanställ trovärdiga picks med edge > 0 över alla marknader,
    sorterade på edge%.

    Detta är "find me the best bets right now"-listan — bortom marknad.
    Filtrerar bort longshot-brus (se _is_credible_edge).
    """
    out: list[dict[str, Any]] = []
    for p in picks:
        for market_key, mk in p["markets"].items():
            if not _is_credible_edge(mk):
                continue
            out.append({
                "name": p["name"],
                "market": market_key,
                "prob": mk["prob"],
                "fairOdds": mk.get("fairOdds"),
                "realSSOdds": mk.get("realSSOdds"),
                "edgePct": mk["edgePct"],
                "kellyFraction": mk["kellyFraction"],
                "recommendedStakePct": mk["recommendedStakePct"],
                "confidence": mk["confidence"],
            })
    out.sort(key=lambda x: -x["edgePct"])
    return out[:k]
