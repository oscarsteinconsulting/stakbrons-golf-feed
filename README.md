# Stakbrons Golf Feed

Publik feed som driver iOS-appen **Stakbrons Golf Odds**.
GitHub Actions kör `scripts/generate.py` varje morgon kl 06:00 UTC (08:00 svensk
sommartid) och pushar färska rapporter till `data/reports.json`.

**Feed-URL (för appen):**
```
https://raw.githubusercontent.com/oscarsteinconsulting/stakbrons-golf-feed/main/data/reports.json
```

## Vad scriptet gör

1. Hämtar ESPN PGA + LPGA scoreboard
2. För varje pågående tävling: bestämmer aktuell runda från ESPN-statusen
3. Genererar dagsrapporter för alla rundor från R1 till aktuell (så fredag-tabben
   stannar tillgänglig på lördag morgon)
4. Skriver `data/reports.json` med headlines, insights och 10 spel-tips per runda

## Köra lokalt

```bash
python3 scripts/generate.py
cat data/reports.json | jq
```

Inga externa Python-bibliotek krävs — bara standardbiblioteket.

## Manuell trigger på GitHub

Actions → Generate Stakbrons Golf Feed → Run workflow

## Struktur

```
.
├── .github/workflows/daily.yml   # Cron-schemalagd Action
├── scripts/generate.py           # Feed-generator (Python)
├── data/reports.json             # Live feed (skrivs av scriptet)
└── README.md
```

## Disclaimer

Innehållet är redaktionell analys för informationssyften. Odds är indikativa och
måste verifieras hos Svenska Spel innan spel. 18+, Stödlinjen 020-81 91 00.
