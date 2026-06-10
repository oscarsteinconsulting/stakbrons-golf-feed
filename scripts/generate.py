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
import json
import sys
import urllib.request
from pathlib import Path

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/golf"
TIMEOUT = 15

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

def headline_text(round_num: int, players: list, is_current: bool) -> str:
    if not players:
        return f"R{round_num} live — leaderboard hämtas live från ESPN"
    leader = players[0]
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
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return {
        "kind": "daily",
        "locked": False,
        "generatedAt": now_iso,
        "headline": headline_text(round_num, players, is_current),
        "insights": [
            {
                "icon": "list.number",
                "h": f"Leaderboard live (R{round_num})",
                "b": leaderboard_body(players),
            },
            {
                "icon": "scope",
                "h": "Spridning i toppen",
                "b": spread_body(players),
            },
            {
                "icon": "wave.3.right",
                "h": "Auto-genererat",
                "b": (
                    "Den här rapporten genereras automatiskt varje morgon kl 08:00 svensk tid "
                    "från ESPN-leaderboarden via en publik feed på GitHub. Spelen baseras på "
                    "spelarpositioner och en enkel implicit-sannolikhetsmodell — odds är "
                    "indikativa och måste verifieras mot Svenska Spel innan spel."
                ),
            },
        ],
        "top5": [],
        "bets": make_tips(round_num, players),
    }


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


def build_tournament_entry(board: dict) -> dict | None:
    current = current_round(board["statusDetail"])
    state = board["state"]
    status_label = status_to_label(state, board["statusDetail"])

    reports = {}
    if current >= 1:
        for r in range(1, current + 1):
            reports[day_key(r)] = make_daily_report(r, board, is_current=(r == current))

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

    for tour, label in (("pga", "PGA Tour"), ("lpga", "LPGA")):
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
            print(
                f"  ✓ {entry['name']} ({entry['tour']}) — status={entry['status']}, "
                f"R{entry['currentRound']}, rapporter: {list(entry['reports'].keys())}"
            )

    out_path = Path("data/reports.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\nSkrev {out_path} med {len(output['tournaments'])} tävling(ar) "
          f"({sum(len(t['reports']) for t in output['tournaments'])} rapporter totalt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
