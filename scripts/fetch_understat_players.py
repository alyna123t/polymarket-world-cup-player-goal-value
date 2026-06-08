#!/usr/bin/env python3
"""Fetch player stats from a single free source (Understat) and write CSV for WCPGV."""

from __future__ import annotations

import argparse
import csv
import re
import unicodedata
from pathlib import Path

import requests

LEAGUES = ["EPL", "La_liga", "Bundesliga", "Serie_A", "Ligue_1"]


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-zA-Z0-9\s\-']", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def parse_seasons(s: str) -> list[int]:
    out = []
    for part in s.split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    if not out:
        raise ValueError("No seasons provided")
    return out


def recency_weight(season: int, seasons: list[int]) -> float:
    # seasons expected newest -> oldest
    idx = seasons.index(season)
    base = [1.0, 0.7, 0.5, 0.35]
    return base[idx] if idx < len(base) else 0.25


def fetch_league_players(session: requests.Session, league: str, season: int) -> list[dict]:
    ua = {"User-Agent": "Mozilla/5.0"}
    referer = f"https://understat.com/league/{league}/{season}"
    page = session.get(referer, headers=ua, timeout=30)
    page.raise_for_status()

    api = f"https://understat.com/getLeagueData/{league}/{season}"
    resp = session.get(
        api,
        headers={
            **ua,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    return payload.get("players", [])


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch Understat top-5 league players for WCPGV")
    ap.add_argument("--seasons", default="2026,2025,2024", help="Comma-separated seasons, newest first")
    ap.add_argument("--min-minutes", type=int, default=300)
    ap.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent.parent / "data" / "understat_players_recent_top5.csv"),
    )
    args = ap.parse_args()

    seasons = parse_seasons(args.seasons)
    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()

    # weighted aggregate by normalized player name
    agg: dict[str, dict] = {}
    total_rows = 0

    for season in seasons:
        sw = recency_weight(season, seasons)
        for league in LEAGUES:
            players = fetch_league_players(session, league, season)
            for p in players:
                try:
                    name = str(p.get("player_name", "")).strip()
                    if not name:
                        continue
                    minutes = float(p.get("time", 0) or 0)
                    games = float(p.get("games", 0) or 0)
                    goals = float(p.get("goals", 0) or 0)
                    npg = float(p.get("npg", goals) or goals)
                    pos = str(p.get("position", "")).strip()
                    team = str(p.get("team_title", "")).strip()
                except Exception:
                    continue

                if minutes < args.min_minutes or games <= 0:
                    continue

                total_rows += 1
                key = normalize_name(name)
                if key not in agg:
                    agg[key] = {
                        "player_name": name,
                        "minutes_w": 0.0,
                        "games_w": 0.0,
                        "goals_w": 0.0,
                        "npg_w": 0.0,
                        "w": 0.0,
                        "position": pos,
                        "team_title": team,
                        "league": league,
                        "latest_season": season,
                        "latest_minutes": minutes,
                    }

                a = agg[key]
                a["minutes_w"] += minutes * sw
                a["games_w"] += games * sw
                a["goals_w"] += goals * sw
                a["npg_w"] += npg * sw
                a["w"] += sw

                if season > a["latest_season"] or (season == a["latest_season"] and minutes > a["latest_minutes"]):
                    a["player_name"] = name
                    a["position"] = pos
                    a["team_title"] = team
                    a["league"] = league
                    a["latest_season"] = season
                    a["latest_minutes"] = minutes

    rows: list[dict] = []
    for a in agg.values():
        if a["w"] <= 0:
            continue
        rows.append(
            {
                "player_name": a["player_name"],
                "minutes": int(round(a["minutes_w"] / a["w"])),
                "games": int(round(a["games_w"] / a["w"])),
                "goals": round(a["goals_w"] / a["w"], 3),
                "npg": round(a["npg_w"] / a["w"], 3),
                "position": a["position"],
                "team_title": a["team_title"],
                "league": a["league"],
                "season": ",".join(str(s) for s in seasons),
                "source": "understat",
            }
        )

    rows.sort(key=lambda r: (r["league"], -int(r["minutes"]), r["player_name"]))

    fields = [
        "player_name",
        "minutes",
        "games",
        "goals",
        "npg",
        "position",
        "team_title",
        "league",
        "season",
        "source",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(f"Wrote {len(rows)} players to {out_path} from understat_rows={total_rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
