#!/usr/bin/env python3
"""
Stakbrons Golf Odds — daglig feed-generator.

Körs av GitHub Actions varje morgon kl 06:00 UTC (08:00 svensk tid).
Hämtar ESPN PGA + LPGA scoreboard, bygger en JSON-feed med rapporter för varje
tävling och runda, och skriver till data/reports.json. Pushas sen tillbaka
till repot — appen läser raw.githubusercontent.com-URL:n.

Kör lokalt med:  python3 scripts/generate.py
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/golf"
TIMEOUT = 15

# (endpoint, label) — ESPN-stöd för golf-tour API:n
TOURS = [
    ("pga",  "PGA Tour"),
    ("lpga", "LPGA"),
    ("eur",  "DP World Tour"),
    ("liv",  "LIV Golf"),
]

# Anthropic Claude — anropas valfritt om ANTHROPIC_API_KEY finns
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
USE_LLM = bool(ANTHROPIC_API_KEY)
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5")
LLM_CACHE_DIR = Path("data/.llm-cache")

# ----------------------------------------------------------------------------
# ESPN helpers
# ----------------------------------------------------------------------------

def fetch_scoreboard(tour: str, dates: str | None = None) -> dict:
    """Hämta scoreboard. Med `dates="YYYYMMDD-YYYYMMDD"` hämtas events i intervallet."""
    url = f"{ESPN_BASE}/{tour}/scoreboard"
    if dates:
        url += f"?dates={dates}"
    req = urllib.request.Request(url, headers={"User-Agent": "stakbrons-feed/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
        return json.load(response)


def fetch_all_relevant_events(tour: str) -> list[dict]:
    """Hämtar både nuvarande scoreboard OCH senaste veckans events (för Final-resultat).
    Deduplikerar på event-id."""
    today = dt.datetime.now(dt.timezone.utc)
    week_back = today - dt.timedelta(days=7)
    week_ahead = today + dt.timedelta(days=14)
    date_range = f"{week_back:%Y%m%d}-{week_ahead:%Y%m%d}"

    events: dict[str, dict] = {}
    for kwargs in ({"dates": None}, {"dates": date_range}):
        try:
            data = fetch_scoreboard(tour, **kwargs)
        except Exception as exc:
            print(f"  WARNING: kunde inte hämta {tour} (dates={kwargs.get('dates')}): {exc}",
                  file=sys.stderr)
            continue
        for ev in data.get("events", []):
            eid = ev.get("id", ev.get("name", ""))
            events.setdefault(eid, ev)
    return list(events.values())


def current_round(status_detail: str) -> int:
    """Räkna ut vilken dagsrapport som ska visas baserat på ESPN-statusen.

    Logik (matchar iOS-appens ReportGenerator.currentRound):
    - "Round X - In Progress" → X
    - "Round X - Play Complete" → X+1 (mellan rundor, visa nästa)
    - "Final" → 4
    """
    s = status_detail.lower()
    if "final" in s:
        return 4
    between = any(x in s for x in ("complete", "suspended", "delayed", "finished"))
    if between:
        if "round 1" in s:
            return 2
        if "round 2" in s:
            return 3
        if "round 3" in s:
            return 4
        if "round 4" in s:
            return 4
    if "round 4" in s:
        return 4
    if "round 3" in s:
        return 3
    if "round 2" in s:
        return 2
    if "round 1" in s:
        return 1
    return 0


def day_key(round_num: int) -> str:
    """R1=torsdag, R2=fredag, R3=lordag, R4=sondag."""
    return ["torsdag", "fredag", "lordag", "sondag"][max(0, round_num - 1)]


def slugify(name: str) -> str:
    """Förenkla namn till stabilt id som matchar iOS-appens slugify."""
    s = name.lower()
    if " pres. by " in s:
        s = s.split(" pres. by ")[0]
    if s.startswith("the "):
        s = s[4:]
    # Behåll bokstäver/siffror, byt mellanslag och andra tecken mot bindestreck
    cleaned = []
    last_dash = True
    for c in s:
        if c.isalnum():
            cleaned.append(c)
            last_dash = False
        elif not last_dash:
            cleaned.append("-")
            last_dash = True
    out = "".join(cleaned).strip("-")
    return f"live-{out}" if out else "live-unknown"


def pretty_name(name: str) -> str:
    """Plockar bort sponsorprefix från ESPN-namnet."""
    s = name
    if " pres. by " in s.lower():
        idx = s.lower().index(" pres. by ")
        s = s[:idx]
    if s.lower().startswith("the "):
        s = s[4:]
    return s[:1].upper() + s[1:] if s else s


def parse_event(event: dict, tour_label: str) -> dict:
    """Plockar ut det vi behöver från ett ESPN-event."""
    comp = event["competitions"][0]
    status = comp["status"]["type"]
    state = status.get("state", "pre")
    detail = status.get("detail") or status.get("description") or "—"

    competitors = comp.get("competitors", [])
    players = []
    for c in competitors:
        ath = c.get("athlete", {}) or {}
        flag = ath.get("flag") or {}
        linescores = c.get("linescores") or []
        rounds = []
        for ls in linescores:
            dv = ls.get("displayValue")
            if dv is None and ls.get("value") is not None:
                dv = str(int(ls.get("value")))
            rounds.append(dv or "-")
        try:
            total_value = float(c.get("score", 999))
        except (TypeError, ValueError):
            total_value = 999.0
        players.append({
            "id": c.get("id", ""),
            "name": ath.get("displayName") or "?",
            "country": (flag.get("alt") if flag else None),
            "position": ((c.get("status") or {}).get("position") or {}).get("displayName", ""),
            "totalDisplay": c.get("score") or "—",
            "totalValue": total_value,
            "rounds": rounds,
        })
    players.sort(key=lambda p: p["totalValue"])

    course = comp.get("course") or {}
    venue = comp.get("venue") or {}
    return {
        "name": event.get("name", "?"),
        "tour": tour_label,
        "statusDetail": detail,
        "state": state,
        "players": players,
        "startDate": event.get("date"),
        "venue": venue.get("fullName"),
        "par": course.get("par"),
        "yards": course.get("yardage"),
    }


# ----------------------------------------------------------------------------
# Headline / insight / tips
# ----------------------------------------------------------------------------

def headline_text(round_num: int, players: list, is_current: bool, is_final: bool = False) -> str:
    if not players:
        return f"R{round_num} live — leaderboard hämtas live från ESPN"
    leader = players[0]
    if is_final and round_num == 4:
        margin_text = ""
        if len(players) >= 2:
            gap = abs(players[1]["totalValue"] - leader["totalValue"])
            n = int(round(gap))
            if n == 0:
                margin_text = " — playoff/oavgjord"
            elif n == 1:
                margin_text = f" — vinst med ett slag över {players[1]['name']}"
            else:
                margin_text = f" — vinst med {n} slag över {players[1]['name']}"
        return f"Slutresultat: {leader['name']} vann på {leader['totalDisplay']}{margin_text}"
    label = "live" if is_current else "utfall"
    cluster = ""
    if len(players) >= 2:
        next_p = players[1]
        gap = abs(next_p["totalValue"] - leader["totalValue"])
        if gap < 0.5:
            cluster = " — flera spelare delar ledningen"
        else:
            n = int(round(gap))
            gap_text = "ett slag" if n == 1 else f"{n} slag"
            cluster = f" — {next_p['name']} jagar {gap_text} bakom"
    return f"R{round_num} {label}: {leader['name']} leder {leader['totalDisplay']}{cluster}"


# ----------------------------------------------------------------------------
# Svensk-callouts
# ----------------------------------------------------------------------------

SWEDISH_VARIANTS = {"sweden", "sverige", "swe", "se"}


def swedish_players(players: list) -> list:
    """Returnera spelare med svensk landstilhörighet i topp 60."""
    out = []
    for p in players[:60]:
        cc = (p.get("country") or "").strip().lower()
        if cc in SWEDISH_VARIANTS:
            out.append(p)
    return out


def swedish_insight(players: list) -> dict | None:
    """Bygg en 'Svenskkollen'-insight om minst en svensk är i fältet."""
    swedes = swedish_players(players)
    if not swedes:
        return None
    rows = []
    for p in swedes[:6]:
        pos = p["position"] or "—"
        cluster = ""
        if p["totalValue"] <= 0:
            cluster = "  (i röda siffror)"
        rows.append(f"{pos}  {p['name']}  {p['totalDisplay']}{cluster}")
    if any(p["position"].startswith(("1", "2", "3", "4", "5")) and not p["position"].startswith(("10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "20")) for p in swedes):
        prefix = "Stark svensk insats."
    elif swedes:
        prefix = f"{len(swedes)} svensk{'a' if len(swedes) > 1 else ''} kvar i fältet."
    else:
        prefix = ""
    return {
        "icon": "flag.fill",
        "h": "🇸🇪 Svenskkollen",
        "b": (prefix + "\n\n" if prefix else "") + "\n".join(rows),
    }


# ----------------------------------------------------------------------------
# LLM (Anthropic Claude) — valfritt
# ----------------------------------------------------------------------------

def _llm_cache_path(key: str) -> Path:
    h = hashlib.sha256(key.encode()).hexdigest()[:16]
    LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return LLM_CACHE_DIR / f"{h}.txt"


def llm_generate(prompt: str, max_tokens: int = 300, cache_key: str | None = None) -> str | None:
    """Anropa Anthropic Claude. Returnerar None om ANTHROPIC_API_KEY saknas eller anropet failar."""
    if not USE_LLM:
        return None
    # Enkel disk-cache: samma leaderboard-snapshot ger samma text utan att anropa API:t igen
    if cache_key:
        cp = _llm_cache_path(cache_key)
        if cp.exists():
            return cp.read_text(encoding="utf-8")

    body = {
        "model": LLM_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
        text = data["content"][0]["text"].strip()
        if cache_key:
            _llm_cache_path(cache_key).write_text(text, encoding="utf-8")
        return text
    except Exception as e:
        print(f"  ! LLM-anrop failade: {e}", file=sys.stderr)
        return None


def llm_headline(round_num: int, board: dict, is_final: bool) -> str | None:
    top = board["players"][:12]
    if not top:
        return None
    leader = top[0]
    state = "Slutresultat" if is_final else f"R{round_num} live"
    rows = "\n".join(
        f"{p['position'] or '—'}  {p['name']}  {p['totalDisplay']}  ({p.get('country') or '?'})"
        for p in top
    )
    swedish = ", ".join(p["name"] for p in swedish_players(board["players"])[:5])
    prompt = (
        "Du är en svensk sportreporter som skriver för Stakbrons Golf Odds. "
        "Skriv en kort, kraftfull rubrik på svenska (max 110 tecken) som fångar "
        "leaderboard-situationen. Var konkret med namn och slag. Nämn en svensk om "
        "någon är i topp 10 eller har en intressant position. Undvik klyschor.\n\n"
        f"Tävling: {board['name']} ({board['tour']})\n"
        f"Status: {state}\n"
        f"Ledare: {leader['name']} på {leader['totalDisplay']}\n"
        f"Svenskar i fält: {swedish or 'inga relevanta'}\n\n"
        "Topp 12:\n"
        f"{rows}\n\n"
        "Returnera ENDAST rubriktexten — inga citationstecken, inga prefix."
    )
    key = f"headline:{board['name']}:{round_num}:{is_final}:" + ",".join(
        f"{p['name']}={p['totalDisplay']}" for p in top
    )
    txt = llm_generate(prompt, max_tokens=80, cache_key=key)
    if txt:
        txt = txt.strip().strip('"').strip("'").strip()
    return txt


def llm_executive_summary(round_num: int, board: dict, is_final: bool) -> str | None:
    top = board["players"][:15]
    if not top:
        return None
    rows = "\n".join(
        f"{p['position'] or '—'}  {p['name']}  {p['totalDisplay']}  ({p.get('country') or '?'})"
        for p in top
    )
    state = "Tävlingen är klar." if is_final else f"R{round_num} pågår."
    swedish = ", ".join(p["name"] for p in swedish_players(board["players"])[:5])
    prompt = (
        "Du är en svensk sportreporter som skriver för Stakbrons Golf Odds. "
        "Skriv en kort analytisk sammanfattning på svenska (3-4 meningar) av "
        "leaderboard-situationen. Fokus: vem leder och varför, vilka chasers, "
        "vad som händer härnäst. Nämn relevant svensk om någon är i topp 20. "
        "Skriv saklig sportreportertilll — undvik klyschor och floskler.\n\n"
        f"Tävling: {board['name']} ({board['tour']})\n"
        f"Status: {state}\n"
        f"Svenskar i fält: {swedish or 'inga relevanta'}\n\n"
        "Topp 15:\n"
        f"{rows}\n\n"
        "Returnera ENDAST den analytiska texten."
    )
    key = f"summary:{board['name']}:{round_num}:{is_final}:" + ",".join(
        f"{p['name']}={p['totalDisplay']}" for p in top
    )
    return llm_generate(prompt, max_tokens=400, cache_key=key)


def leaderboard_body(players: list) -> str:
    if not players:
        return "Hämtar..."
    rows = []
    for p in players[:10]:
        pos = p["position"] or "—"
        rows.append(f"{pos}  {p['name']}  {p['totalDisplay']}")
    return "\n".join(rows) + "\n\nKälla: ESPN."


def spread_body(players: list) -> str:
    if len(players) < 10:
        return "För tidigt i tävlingen för spridningsanalys."
    leader = players[0]
    t10 = players[min(9, len(players) - 1)]
    gap = int(round(abs(t10["totalValue"] - leader["totalValue"])))
    within3 = sum(1 for p in players if p["totalValue"] - leader["totalValue"] < 3.0)
    descr = "smal" if within3 <= 5 else "bred"
    return (
        f"Topp 10 ligger inom {gap} slag från ledaren. "
        f"{within3} spelare inom 3 slag — vinnar-poolen är {descr}."
    )


def odds_for_leader(round_num: int) -> float:
    table = {1: 8.0, 2: 5.0, 3: 2.5, 4: 1.5}
    return table.get(round_num, 10.0)


def gap_text(player: dict, leader: dict) -> str:
    gap = abs(player["totalValue"] - leader["totalValue"])
    n = int(round(gap))
    if n == 0:
        return "0 slag (delar ledningen)"
    if n == 1:
        return "ett slag"
    return f"{n} slag"


def probability_text(round_num: int) -> str:
    table = {1: "12-15%", 2: "20-25%", 3: "40-50%", 4: "70-85%"}
    return table.get(round_num, "—")


def make_tips(round_num: int, players: list) -> list:
    if not players:
        return []
    leader = players[0]
    rounds_left = max(1, 4 - round_num + 1)
    tips = []

    # 1. Vinnare på ledare
    tips.append({
        "id": f"auto-r{round_num}-w1",
        "cat": "🤖 Vinnare (uppdaterad)",
        "sel": leader["name"],
        "svs": odds_for_leader(round_num),
        "mkt": 0,
        "units": 1.5,
        "conf": 4 if round_num >= 3 else 3,
        "rat": (
            f"{leader['name']} leder fältet på {leader['totalDisplay']} efter R{round_num}. "
            f"Med {rounds_left} runda(or) kvar är 54-håls-ledare-vinst statistiskt cirka "
            f"{probability_text(round_num)} på PGA Tour. Indikativt pris baserat på position "
            f"och rundor kvar — verifiera linjen hos Svenska Spel."
        ),
    })

    # 2. Vinnare värde — T3-spelare
    if len(players) >= 3:
        value_pick = players[2]
        gap = abs(value_pick["totalValue"] - leader["totalValue"])
        odds = round(min(max(odds_for_leader(round_num) * (1.0 + gap * 0.5), 4.0), 40.0), 2)
        tips.append({
            "id": f"auto-r{round_num}-w2",
            "cat": "🤖 Vinnare (värde)",
            "sel": value_pick["name"],
            "svs": odds,
            "mkt": 0,
            "units": 0.75,
            "conf": 2,
            "rat": (
                f"{value_pick['name']} ligger {gap_text(value_pick, leader)} bakom ledaren. "
                f"På en attack-runda kan han/hon ta sig in i finalbollen — indikativt longshot-pris."
            ),
        })

    # 3-4. Topp 5 — T2, T4
    for offset in (1, 3):
        if len(players) > offset:
            p = players[offset]
            gap = abs(p["totalValue"] - leader["totalValue"])
            odds = round(min(2.2 + gap * 0.3, 5.0) if round_num < 3 else min(1.7 + gap * 0.2, 3.5), 2)
            tips.append({
                "id": f"auto-r{round_num}-t5-{offset}",
                "cat": "🤖 Topp 5",
                "sel": p["name"],
                "svs": odds,
                "mkt": 0,
                "units": 1.5,
                "conf": 4 if round_num >= 3 else 3,
                "rat": (
                    f"{p['name']} ({p['totalDisplay']}) sitter i topp-skiktet "
                    f"{gap_text(p, leader)} bakom ledaren. "
                    f"Med {rounds_left} runda(or) kvar är topp-5 ett rimligt scenario från denna position."
                ),
            })

    # 5-6. Topp 10 — T6, T8
    for offset in (5, 7):
        if len(players) > offset:
            p = players[offset]
            gap = abs(p["totalValue"] - leader["totalValue"])
            odds = round(min(1.8 + gap * 0.2, 3.0) if round_num < 3 else min(1.4 + gap * 0.1, 2.2), 2)
            tips.append({
                "id": f"auto-r{round_num}-t10-{offset}",
                "cat": "🤖 Topp 10",
                "sel": p["name"],
                "svs": odds,
                "mkt": 0,
                "units": 1,
                "conf": 3,
                "rat": (
                    f"{p['name']} ({p['totalDisplay']}) ligger inom topp-10 just nu. "
                    f"Stark position att hålla över återstående {rounds_left} runda(or)."
                ),
            })

    # 7-8. Topp 20 — T15, T19
    for offset in (14, 18):
        if len(players) > offset:
            p = players[offset]
            gap = abs(p["totalValue"] - leader["totalValue"])
            odds = round(min(1.5 + gap * 0.1, 2.5) if round_num < 3 else min(1.2 + gap * 0.05, 1.8), 2)
            tips.append({
                "id": f"auto-r{round_num}-t20-{offset}",
                "cat": "🤖 Topp 20",
                "sel": p["name"],
                "svs": odds,
                "mkt": 0,
                "units": 1,
                "conf": 3,
                "rat": (
                    f"{p['name']} ({p['totalDisplay']}) på topp-20-gränsen. "
                    f"Med {rounds_left} runda(or) kvar krävs bara stabilitet för att hänga kvar."
                ),
            })

    # 9. Lägsta runda — T12-spelare
    if len(players) > 12:
        attack = players[11]
        tips.append({
            "id": f"auto-r{round_num}-low",
            "cat": f"🤖 Lägsta runda R{round_num + 1}",
            "sel": attack["name"],
            "svs": 12.0,
            "mkt": 0,
            "units": 0.5,
            "conf": 2,
            "rat": (
                f"{attack['name']} ({attack['totalDisplay']}) spelar utan press och har taket att "
                f"klippa en attack-runda. Stort longshot-pris för liten insats."
            ),
        })

    # 10. Bogeyfri runda — ledaren
    if leader:
        tips.append({
            "id": f"auto-r{round_num}-bf",
            "cat": "🤖 Bogeyfri runda",
            "sel": leader["name"],
            "svs": 5.5,
            "mkt": 0,
            "units": 0.5,
            "conf": 2,
            "rat": (
                f"{leader['name']} är en av få spelare med konsekventa rundor hittills — "
                f"bogeyfri är en realistisk possibility. Liten insats, fin uppsida om "
                f"approach-spelet håller."
            ),
        })

    return tips


def make_daily_report(round_num: int, board: dict, is_current: bool) -> dict:
    players = board["players"]
    is_final = "final" in board["statusDetail"].lower() or board["state"] == "post"
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    leaderboard_header = (
        "Slutresultat" if (is_final and round_num == 4)
        else f"Leaderboard live (R{round_num})"
    )

    # LLM-genererad rubrik om tillgänglig; mekaniskt fallback annars
    headline = llm_headline(round_num, board, is_final) or headline_text(round_num, players, is_current, is_final=is_final)

    # Bygg insights — exec summary först (LLM eller mekanisk spridning),
    # leaderboard, svenskkollen (om relevant), metodologi
    insights = []

    summary = llm_executive_summary(round_num, board, is_final)
    if summary:
        insights.append({
            "icon": "newspaper",
            "h": "Reportage",
            "b": summary,
        })

    insights.append({
        "icon": "list.number",
        "h": leaderboard_header,
        "b": leaderboard_body(players),
    })

    sve = swedish_insight(players)
    if sve:
        insights.append(sve)

    insights.append({
        "icon": "scope",
        "h": "Spridning i toppen",
        "b": spread_body(players),
    })

    insights.append({
        "icon": "wave.3.right",
        "h": "Auto-genererat",
        "b": (
            "Den här rapporten genereras automatiskt varje morgon kl 08:00 svensk tid "
            "från ESPN-leaderboarden via en publik feed på GitHub. "
            + ("Reportage och rubrik skrivs av Claude. " if USE_LLM else "")
            + "Spelen baseras på spelarpositioner och en enkel implicit-sannolikhetsmodell — "
            "odds är indikativa och måste verifieras mot Svenska Spel innan spel."
        ),
    })

    return {
        "kind": "daily",
        "locked": False,
        "generatedAt": now_iso,
        "headline": headline,
        "insights": insights,
        "top5": [],
        "bets": make_tips(round_num, players),
    }


# ----------------------------------------------------------------------------
# Historik per tävling
# ----------------------------------------------------------------------------

def save_history(entry: dict, board: dict) -> Path | None:
    """När en tävling blir final, spara snapshot till data/history/{year}/{slug}.json.
    Filen skapas med årstal från startdatum och kompletteras med fullt leaderboard +
    rubriker per dag. Säkert att köra flera gånger — vi skriver över."""
    if entry["status"] != "final":
        return None
    start = entry.get("startDate") or ""
    year = start[:4] if start[:4].isdigit() else dt.date.today().strftime("%Y")

    out_dir = Path("data/history") / year
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{entry['id']}.json"

    snapshot = {
        "id": entry["id"],
        "name": entry["name"],
        "tour": entry["tour"],
        "course": entry["course"],
        "dates": entry.get("dates"),
        "par": entry.get("par"),
        "yards": entry.get("yards"),
        "startDate": entry.get("startDate"),
        "snapshotAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "headlines": {day: r.get("headline") for day, r in entry["reports"].items()},
        "finalLeaderboard": [
            {
                "position": p["position"] or "—",
                "name": p["name"],
                "country": p.get("country"),
                "totalDisplay": p["totalDisplay"],
                "totalValue": p["totalValue"],
                "rounds": p.get("rounds", []),
            }
            for p in board["players"][:80]
        ],
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, ensure_ascii=False)
    return out_path


def update_history_index() -> None:
    """Indexera alla historiska filer i data/history/index.json för enkel listning."""
    root = Path("data/history")
    if not root.exists():
        return
    entries = []
    for year_dir in sorted(root.iterdir()):
        if not year_dir.is_dir():
            continue
        for f in sorted(year_dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                entries.append({
                    "id": d["id"],
                    "name": d["name"],
                    "tour": d.get("tour"),
                    "dates": d.get("dates"),
                    "year": year_dir.name,
                    "path": str(f.relative_to(root.parent)),
                    "snapshotAt": d.get("snapshotAt"),
                    "winnerHeadline": (d.get("headlines") or {}).get("sondag"),
                })
            except Exception:
                continue
    index_path = root / "index.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "entries": entries,
        }, f, indent=2, ensure_ascii=False)


# ----------------------------------------------------------------------------
# Main pipeline
# ----------------------------------------------------------------------------

def status_to_label(state: str, status_detail: str) -> str:
    s = status_detail.lower()
    if "final" in s or state == "post":
        return "final"
    if state == "in":
        return "live"
    return "upcoming"


def _starts_within_days(start_iso: str | None, max_days: int = 7) -> bool:
    """True om event-startdatumet ligger inom intervallet [idag, idag+max_days]."""
    if not start_iso:
        return False
    try:
        # ESPN-format: "2026-06-11T04:00Z"
        start = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = dt.datetime.now(dt.timezone.utc)
    delta = (start - now).days
    return -1 <= delta <= max_days   # tillåt redan-startat (delta=-1) också


def make_preview_report(board: dict) -> dict:
    """Onsdag-preview för upcoming tournament.

    LLM-version: full analys-text. Fallback: mall-baserad med fält-overview,
    svensk-callout och 5 namn ur fältet som "intressanta att hålla koll på".
    """
    players = board["players"]
    field_size = len(players)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    # LLM-rubrik
    headline = llm_preview_headline(board)
    if not headline:
        course = board.get("venue") or "veckans bana"
        headline = f"Inför {pretty_name(board['name'])}: fältet samlas på {course}"

    insights: list[dict] = []

    # LLM-skrivet reportage
    summary = llm_preview_summary(board)
    if summary:
        insights.append({"icon": "newspaper", "h": "Reportage", "b": summary})

    # Bana & datum-info
    course = board.get("venue") or "—"
    par = board.get("par") or 0
    yards = board.get("yards") or 0
    course_lines = [f"Bana: {course}"]
    if par > 0:
        course_lines.append(f"Par {par}" + (f" · {yards} yards" if yards > 0 else ""))
    start_iso = board.get("startDate")
    if start_iso:
        try:
            start = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            course_lines.append(f"Starttid runda 1: {start.strftime('%A %d %B %Y')}")
        except Exception:
            pass
    insights.append({
        "icon": "flag.fill",
        "h": "Banan & schemat",
        "b": "\n".join(course_lines),
    })

    # Fältöversikt
    insights.append({
        "icon": "person.3.fill",
        "h": "Fältöversikt",
        "b": f"{field_size} spelare anmälda. Första utslag kl ~13:00 svensk tid på torsdag. Fullständig fältlista hämtas live från ESPN.",
    })

    # Svensk-callout om svenskar finns i fältet
    sve = swedish_insight(players)
    if sve:
        sve = {**sve, "h": "🇸🇪 Svenskar i fältet"}
        insights.append(sve)

    # Metodologi
    insights.append({
        "icon": "wave.3.right",
        "h": "Auto-genererat",
        "b": (
            "Förhandsrapport genereras automatiskt så fort en tävling är inom en vecka från start. "
            + ("Rubrik och reportage skrivs av Claude. " if USE_LLM else "")
            + "Spelen baseras på fältöversikt — odds är indikativa och måste verifieras mot Svenska Spel innan spel."
        ),
    })

    return {
        "kind": "preview",
        "locked": False,
        "generatedAt": now_iso,
        "headline": headline,
        "insights": insights,
        "top5": [],
        "bets": make_preview_bets(players),
    }


def make_preview_bets(players: list) -> list:
    """5-10 outright-tips baserat på fältordningen ESPN ger.
    Utan rankings är detta en grov approximation — labelas tydligt."""
    tips = []
    for i, p in enumerate(players[:6]):
        odds = round(8 + i * 3.5, 1)
        tips.append({
            "id": f"auto-preview-w{i+1}",
            "cat": "🤖 Outright (förhand)",
            "sel": p["name"],
            "svs": odds,
            "mkt": 0,
            "units": 1.0 if i < 2 else 0.5,
            "conf": 3 if i < 2 else 2,
            "rat": (
                f"{p['name']} står med i fältet. Indikativt pris baserat på fält-position; "
                "verifiera linjen och formen hos Svenska Spel innan spel."
            ),
        })
    return tips


def llm_preview_headline(board: dict) -> str | None:
    if not USE_LLM:
        return None
    top = board["players"][:15]
    rows = "\n".join(f"{p['name']} ({p.get('country') or '?'})" for p in top)
    swedish = ", ".join(p["name"] for p in swedish_players(board["players"])[:5])
    prompt = (
        "Du är en svensk sportreporter som skriver förhandsrubriker för Stakbrons Golf Odds. "
        "Skriv en kort, kraftfull rubrik på svenska (max 110 tecken) inför kommande "
        "golf-tävling. Var konkret — nämn bana, tour eller intressant spelare. Nämn en "
        "svensk om någon är i fältet. Undvik klyschor.\n\n"
        f"Tävling: {board['name']} ({board['tour']})\n"
        f"Bana: {board.get('venue') or 'okänd'}\n"
        f"Svenskar i fält: {swedish or 'inga relevanta'}\n\n"
        "Första 15 i fältlistan från ESPN:\n"
        f"{rows}\n\n"
        "Returnera ENDAST rubriktexten."
    )
    key = f"preview-headline:{board['name']}:{','.join(p['name'] for p in top[:5])}"
    txt = llm_generate(prompt, max_tokens=80, cache_key=key)
    if txt:
        txt = txt.strip().strip('"').strip("'").strip()
    return txt


def llm_preview_summary(board: dict) -> str | None:
    if not USE_LLM:
        return None
    top = board["players"][:20]
    rows = "\n".join(f"{p['name']} ({p.get('country') or '?'})" for p in top)
    swedish = ", ".join(p["name"] for p in swedish_players(board["players"])[:5])
    prompt = (
        "Du är en svensk sportreporter som skriver förhandsanalyser för Stakbrons Golf Odds. "
        "Skriv en kort förhandsanalys (3-4 meningar) inför kommande tävling. Fokus: tour, "
        "intressanta namn, eventuella svenskar, vad som är värt att hålla koll på. "
        "Saklig sportreportertilll, undvik klyschor.\n\n"
        f"Tävling: {board['name']} ({board['tour']})\n"
        f"Bana: {board.get('venue') or 'okänd'}\n"
        f"Svenskar i fält: {swedish or 'inga relevanta'}\n\n"
        "Första 20 i fältlistan från ESPN:\n"
        f"{rows}\n\n"
        "Returnera ENDAST analysen."
    )
    key = f"preview-summary:{board['name']}:{','.join(p['name'] for p in top[:5])}"
    return llm_generate(prompt, max_tokens=400, cache_key=key)


def build_tournament_entry(board: dict) -> dict | None:
    current = current_round(board["statusDetail"])
    state = board["state"]
    status_label = status_to_label(state, board["statusDetail"])

    reports = {}
    if current >= 1:
        for r in range(1, current + 1):
            reports[day_key(r)] = make_daily_report(r, board, is_current=(r == current))
    elif state == "pre" and _starts_within_days(board.get("startDate"), max_days=7):
        # Upcoming-event som börjar inom en vecka — generera onsdag-preview
        reports["onsdag"] = make_preview_report(board)

    return {
        "id": slugify(board["name"]),
        "name": pretty_name(board["name"]),
        "rawName": board["name"],
        "tour": board["tour"],
        "course": board["venue"] or "—",
        "startDate": board["startDate"],
        "par": board["par"] or 71,
        "yards": board["yards"] or 0,
        "statusDetail": board["statusDetail"],
        "status": status_label,
        "currentRound": current,
        "reports": reports,
    }


def main() -> int:
    output = {
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "tournaments": [],
    }

    print(f"LLM-stöd: {'AKTIVT (' + LLM_MODEL + ')' if USE_LLM else 'AVSTÄNGT (ingen ANTHROPIC_API_KEY)'}")
    print(f"Tours: {[t[1] for t in TOURS]}\n")

    history_saved = []
    for tour, label in TOURS:
        events = fetch_all_relevant_events(tour)
        if not events:
            print(f"  (inga {label}-events hittade)", file=sys.stderr)
            continue
        for event in events:
            board = parse_event(event, label)
            entry = build_tournament_entry(board)
            if entry is None:
                continue
            output["tournaments"].append(entry)

            # Spara historik om tävlingen är klar
            hist_path = save_history(entry, board)
            if hist_path:
                history_saved.append(str(hist_path))

            swedes_count = len(swedish_players(board["players"]))
            sve_tag = f" 🇸🇪 {swedes_count} sv" if swedes_count else ""
            print(
                f"  ✓ {entry['name']} ({entry['tour']}) — status={entry['status']}, "
                f"R{entry['currentRound']}, rapporter: {list(entry['reports'].keys())}{sve_tag}"
            )

    if history_saved:
        print(f"\n📚 Sparade historik: {len(history_saved)} st")
        for p in history_saved:
            print(f"    {p}")
    update_history_index()

    out_path = Path("data/reports.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nSkrev {out_path} med {len(output['tournaments'])} tävling(ar) "
          f"({sum(len(t['reports']) for t in output['tournaments'])} rapporter totalt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
