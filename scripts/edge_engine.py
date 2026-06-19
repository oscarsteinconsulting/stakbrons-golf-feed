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
import math
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


# μ för spelare som saknas helt i den oberoende rankingen (utanför OWGR topp
# 600 / DG). Dessa är i praktiken Monday-qualifiers, amatörer och lokala pros —
# de svagaste i fältet. Vi modellerar dem som tydligt svaga (men inte hopplösa)
# istället för medel, annars övervärderar modellen dem grovt.
UNRANKED_MU = 2.30

# Minsta andel av FÄLTET som den oberoende rankingen (OWGR/DG) måste täcka för
# att vi ska använda den. Under detta (t.ex. ett LPGA-fält i herr-OWGR = 0%)
# faller vi tillbaka på Kambi-implicit istället för att ge alla flat μ.
MIN_FIELD_COVERAGE = 0.40


def mu_from_rank(rank: int | None) -> float:
    """Returnera baseline-μ från world rank. None = oberoende-oranked = svag."""
    if rank is None or rank <= 0:
        return UNRANKED_MU
    for max_rank, mu in RANK_TO_MU:
        if rank <= max_rank:
            return mu
    return 3.00


def mu_from_points(points: float) -> float:
    """Kontinuerlig μ från OWGR pointsAverage — mycket finkornigare än de 8
    rank-bucketsen, vilket är avgörande för att differentiera topp-tiern
    (annars klustras alla favoriter på samma vinst-sannolikhet).

    Kalibrerad logaritmiskt mot OWGR-skalan:
        pointsAverage 16 (Scheffler-nivå) → μ ≈ -2.2 (elit)
        pointsAverage 5  (topp 10)        → μ ≈ -1.2
        pointsAverage 2  (topp 50)        → μ ≈ -0.4
        pointsAverage 1  (topp 150)       → μ ≈ +0.2
        pointsAverage 0.5                 → μ ≈ +0.8
    Formel: μ = 0.2 − 0.86·ln(points), klampad till [-2.5, +2.3].
    """
    if points <= 0:
        return UNRANKED_MU
    mu = 0.2 - 0.86 * math.log(points)
    return max(-2.5, min(2.3, mu))


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


def baseline_mu(normalized: str, rankings: dict[str, int],
                points: dict[str, float] | None) -> float:
    """Baseline-μ för en spelare: kontinuerlig OWGR-points om tillgänglig
    (finkornig), annars rank-bucket, annars oranked=svag."""
    if points:
        pts = points.get(normalized)
        if pts is not None:
            return mu_from_points(pts)
    return mu_from_rank(rankings.get(normalized))


def estimate_player_mu(
    *,
    name: str,
    rankings: dict[str, int],
    points: dict[str, float] | None,
    completed_rounds: int,
    score_to_par_so_far: int | None,
) -> tuple[float, str]:
    """Returnerar (μ, källa) för en spelare.

    Strategi:
      - Om vi har ≥1 spelad rond denna vecka → score_to_par / completed_rounds
        (rå current form, perfekt signal denna vecka)
      - Annars baseline från OWGR-points (kontinuerlig) eller rank-bucket
    """
    normalized = normalize_name(name)
    mu_baseline = baseline_mu(normalized, rankings, points)

    if completed_rounds >= 1 and score_to_par_so_far is not None:
        mu_current = score_to_par_so_far / completed_rounds
        # Bayesiansk krympning mot baseline. Rankingen är en stark prior värd
        # ~PRIOR_ROUNDS ronder; en enskild spelad rond är en BRUSIG signal om
        # verklig skicklighet (rond-till-rond-korrelationen i golf är låg).
        #
        # VIKTIGT: ledningen (score-to-par) bärs redan med som STARTPOSITION i
        # simuleringen (completed_score). μ ska därför bara spegla FRAMTIDA
        # skicklighet — annars dubbelräknas en het rond (både som ledning OCH
        # som permanent supernivå). Den gamla 0.6·form-blandningen gav en
        # R1-ledare μ ≈ −3.9/rond → 68% vinst-sannolikhet. Med krympningen
        # landar samma ledare på ~20%, vilket matchar verkligheten. Vikten
        # växer med antal spelade ronder, och när få ronder återstår dominerar
        # försprånget ändå — så sena ledare prissätts fortfarande högt.
        PRIOR_ROUNDS = 10.0
        w = completed_rounds / (PRIOR_ROUNDS + completed_rounds)
        mu = (1.0 - w) * mu_baseline + w * mu_current
        source = f"{completed_rounds}r form {int(round(w * 100))}% + baseline"
        return mu, source

    # Pre-tournament / R1 ej startad → rent baseline
    rank = rankings.get(normalized)
    has_pts = bool(points and normalized in points)
    src = "OWGR-points" if has_pts else (f"rank {rank}" if rank else "oranked")
    return mu_baseline, src


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


def gaussian(rng: random.Random, mu: float, sigma: float) -> float:
    """Standard Box-Muller via random.gauss för portability."""
    return rng.gauss(mu, sigma)


# Kvalgränsen (cut): topp 65 + delade efter 36 hål på de flesta tourer.
CUT_LINE = 65


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

    Returnerar dict {player_name: {"win", "top5", "top10", "top20", "cut"}}.
    "cut" (sannolikhet att klara kvalgränsen) räknas bara när kvalgränsen
    ligger framför oss (innan 36 hål spelats), annars utelämnas den.
    """
    rng = random.Random(seed)
    active = [p for p in players if not p.get("missed_cut", False)]
    n = len(active)
    if n == 0 or remaining_rounds <= 0:
        return {}

    # Ronder kvar tills kvalgränsen avgörs (efter 36 hål = 2 ronder från start).
    # remaining=4 → 2 ronder kvar till cut; remaining=3 → 1; remaining≤2 → cut passerad.
    rounds_to_cut = max(0, remaining_rounds - 2)
    track_cut = rounds_to_cut > 0

    counts: dict[str, dict[str, int]] = {
        p["name"]: {"win": 0, "top5": 0, "top10": 0, "top20": 0, "cut": 0}
        for p in active
    }

    for _ in range(n_sims):
        totals: list[tuple[float, str]] = []
        cut_totals: list[tuple[float, str]] = []
        for p in active:
            score = p.get("completed_score", 0) or 0
            for r in range(remaining_rounds):
                score += gaussian(rng, p["mu"], sigma)
                if track_cut and r + 1 == rounds_to_cut:
                    cut_totals.append((score, p["name"]))  # 36-hålsläget
            totals.append((score, p["name"]))

        totals.sort(key=lambda t: t[0])
        for idx, (_, name) in enumerate(totals):
            position = idx + 1
            if position == 1:
                counts[name]["win"] += 1
            if position <= 5:
                counts[name]["top5"] += 1
            if position <= 10:
                counts[name]["top10"] += 1
            if position <= 20:
                counts[name]["top20"] += 1

        if track_cut:
            cut_totals.sort(key=lambda t: t[0])
            for idx, (_, name) in enumerate(cut_totals):
                if idx + 1 <= CUT_LINE:
                    counts[name]["cut"] += 1

    out: dict[str, dict[str, float]] = {}
    for name, markets in counts.items():
        d = {m: c / n_sims for m, c in markets.items() if m != "cut" or track_cut}
        out[name] = d
    return out


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
    points: dict[str, float] | None = None,
    kambi_markets: dict[str, dict[str, float]] | None = None,
    round_started: bool = True,
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

    # Välj rank-källa FÄLT-MEDVETET. VIKTIGT: blanda INTE OWGR (skala 1-600,
    # världsranking) med Kambi-implicit (skala 1-147, position i fältet) — olika
    # skalor ger inkonsekvent μ.
    #
    # Backtest visade att den oberoende rankingen (OWGR) är HERRARNAS — den
    # täcker 0% av ett LPGA-fält, vilket gav alla damer flat μ och meningslösa
    # (brus-)edges. Vi mäter därför hur stor andel av FÄLTET den oberoende
    # rankingen faktiskt täcker, och faller tillbaka på Kambi-implicit när
    # täckningen är för låg.
    #
    # Prioritet:
    #   1. Oberoende ranking (OWGR/DG) OM den täcker ≥ MIN_FIELD_COVERAGE av
    #      fältet → bryter cirkulariteten. Spelare som saknas = svaga (UNRANKED_MU).
    #   2. Annars Kambi-implicit för hela fältet (cirkulär, men bättre än flat μ
    #      — t.ex. för LPGA där herr-OWGR inte hjälper).
    field_norm = [normalize_name(p["name"]) for p in field
                  if p.get("name") and not p.get("missed_cut")]

    def _in_independent(n: str) -> bool:
        return (bool(rankings) and n in rankings) or (bool(points) and n in points)

    coverage = (sum(1 for n in field_norm if _in_independent(n)) / len(field_norm)
                if field_norm else 0.0)

    merged_rankings: dict[str, int] = {}
    n_independent = 0
    use_points: dict[str, float] | None = None
    if (rankings or points) and coverage >= MIN_FIELD_COVERAGE:
        merged_rankings = dict(rankings) if rankings else {}
        n_independent = len(rankings) if rankings else len(points or {})
        use_points = points
    elif kambi_markets and kambi_markets.get("win"):
        merged_rankings = implicit_rank_from_kambi(kambi_markets["win"])

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
            points=use_points,
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

    # Om vi har oberoende ranking (OWGR/DG) för en meningsfull andel av fältet
    # är edgen icke-cirkulär. Annars härleds μ ur Kambis egna odds.
    field_size = len([p for p in players if not p["missed_cut"]])
    independent = n_independent > 0 and completed_rounds == 0
    payload: dict[str, Any] = {
        "modelVersion": "0.7.0",  # Bayesiansk form-krympning (in-play vinst-prob-fix)
        "simulations": n_sims,
        "remainingRounds": remaining,
        "completedRounds": completed_rounds,
        "roundStarted": round_started,
        "sigmaPerRound": ROUND_STD_DEV,
        "assumedSSVig": ASSUMED_SS_VIG,
        "fieldSize": field_size,
        "hasRealOdds": bool(kambi_markets),
        "independentRanking": independent or completed_rounds >= 1,
        "rankingSource": (
            "OWGR/DG" if n_independent > 0 else "Kambi-implicit (cirkulär)"
        ),
        # picks sorterad på vinst-prob; trimma till topp 40 för Form-vyn
        # (topByMarket/topEdges har redan beräknats på hela fältet).
        "picks": picks[:40],
        "topByMarket": _top_picks_by_market(picks, k=8),
    }
    if kambi_markets:
        # "Bästa edges" — sorterad lista över bästa edges. PRE-ROUND (innan
        # utslag, onsdag): bara slutplaceringar (vinnare + topp 5/10/20).
        # IN-PLAY (torsdag–söndag): alla tillgängliga marknader inkl. kvalgräns.
        payload["topEdges"] = _top_edges(picks, k=10, include_cut=round_started)
    return payload


def _top_picks_by_market(picks: list[dict[str, Any]], k: int) -> dict[str, list[dict[str, Any]]]:
    """Returnera top-k spelare per marknad — för enkel UI-rendering.

    Prioriterar edge% när det finns (riktiga odds), annars bara modellprob.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for market in ("win", "top5", "top10", "top20", "cut"):
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


# Trovärdighetsgränser för "Bästa edges"-listan. Syftet är att bara visa
# picks där modellen faktiskt har signal och edgen är realistisk:
#
#   MIN_CREDIBLE_PROB  — under detta domineras edge% av modellbrus (longshots
#                        där vi inte kan skilja 0.4% från 0.8%).
#   MAX_CREDIBLE_ODDS  — över detta är prissättningen så gles att vår
#                        normalfördelnings-approximation inte är meningsfull.
#   MAX_CREDIBLE_EDGE  — riktiga golf-edges mot en skarp bookmaker (Kambi) är
#                        2-15%, sällan över 20%. En "edge" över 40% är nästan
#                        säkert modellfel (vi övervärderar spelaren), inte alfa.
#                        Vi gömmer dem hellre än lurar användaren att satsa.
MIN_CREDIBLE_PROB = 0.06
MAX_CREDIBLE_ODDS = 26.0
MAX_CREDIBLE_EDGE = 0.40


def _is_credible_edge(mk: dict[str, Any]) -> bool:
    """En edge är trovärdig om modellen har signal (hög nog sannolikhet,
    rimligt odds) OCH edgen är realistisk (inte uppblåst modellbrus)."""
    prob = mk.get("prob") or 0
    odds = mk.get("realSSOdds") or 0
    edge = mk.get("edgePct")
    if edge is None or edge <= 0:
        return False
    if prob < MIN_CREDIBLE_PROB:
        return False
    if odds > MAX_CREDIBLE_ODDS:
        return False
    if edge > MAX_CREDIBLE_EDGE:
        return False
    return True


def _top_edges(picks: list[dict[str, Any]], k: int,
               include_cut: bool = True) -> list[dict[str, Any]]:
    """Sammanställ trovärdiga picks med edge > 0 över alla marknader,
    sorterade på edge%.

    Detta är "find me the best bets right now"-listan — bortom marknad.
    Filtrerar bort longshot-brus (se _is_credible_edge). När `include_cut`
    är False (pre-round) tas kvalgräns-marknaden bort så att endast
    slutplaceringar (vinnare + topp 5/10/20) analyseras.
    """
    out: list[dict[str, Any]] = []
    for p in picks:
        for market_key, mk in p["markets"].items():
            if market_key == "cut" and not include_cut:
                continue
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
