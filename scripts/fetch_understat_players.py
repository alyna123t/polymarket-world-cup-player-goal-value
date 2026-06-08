#!/usr/bin/env python3
"""Build multi-source player dataset for WCPGV.

Sources:
- Understat (player goals/minutes)
- FBref (player goals/minutes via worldfootballR_data RDS mirror)
- openfootball (team scoring rate context)
- football-data.co.uk (team scoring rate context)

Output is one row per player with weighted recent-season aggregates.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import tempfile
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

LEAGUES_UNDERSTAT = ["EPL", "La_liga", "Bundesliga", "Serie_A", "Ligue_1"]
LEAGUE_NAME_MAP = {
    "EPL": "Premier League",
    "La_liga": "La Liga",
    "Bundesliga": "Bundesliga",
    "Serie_A": "Serie A",
    "Ligue_1": "Ligue 1",
}
FBREF_COMP_TO_KEY = {
    "Premier League": "EPL",
    "La Liga": "La_liga",
    "Bundesliga": "Bundesliga",
    "Serie A": "Serie_A",
    "Ligue 1": "Ligue_1",
}
OPENFOOTBALL_LEAGUE_FILES = {
    "EPL": "en.1",
    "La_liga": "es.1",
    "Bundesliga": "de.1",
    "Serie_A": "it.1",
    "Ligue_1": "fr.1",
}
FOOTBALL_DATA_CODES = {
    "EPL": "E0",
    "La_liga": "SP1",
    "Bundesliga": "D1",
    "Serie_A": "I1",
    "Ligue_1": "F1",
}


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-zA-Z0-9\s\-']", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def normalize_team(name: str) -> str:
    s = normalize_name(name)
    # strip common suffix/prefix noise to improve cross-source matching
    s = re.sub(r"\b(fc|cf|ac|sc|afc|ud|cd|ss|as|rc|stade|club|de|la|the)\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_seasons(s: str) -> List[int]:
    out = []
    for x in s.split(","):
        x = x.strip()
        if not x:
            continue
        out.append(int(x))
    if not out:
        raise ValueError("No seasons provided")
    return out


def recency_weights(seasons: List[int]) -> Dict[int, float]:
    base = [1.0, 0.7, 0.5, 0.35, 0.25]
    w: Dict[int, float] = {}
    for i, season in enumerate(seasons):
        w[season] = base[i] if i < len(base) else max(0.2, 0.25 - 0.02 * (i - len(base) + 1))
    return w


def source_weights(s: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        out[k.strip()] = float(v.strip())
    return out


def fetch_understat_league_players(session: requests.Session, league: str, season: int) -> List[dict]:
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


def fetch_fbref_player_rows(session: requests.Session, seasons: List[int], min_minutes: int) -> List[dict]:
    try:
        import pyreadr  # optional dependency
    except Exception:
        print("WARN: pyreadr not installed; skipping FBref source")
        return []

    url = "https://raw.githubusercontent.com/JaseZiv/worldfootballR_data/master/data/fb_big5_advanced_season_stats/big5_player_standard.rds"
    content = session.get(url, timeout=120).content
    with tempfile.NamedTemporaryFile(suffix=".rds") as tmp:
        tmp.write(content)
        tmp.flush()
        df = next(iter(pyreadr.read_r(tmp.name).values()))

    want_end_years = {s + 1 for s in seasons}
    rows: List[dict] = []
    for _, r in df.iterrows():
        try:
            end_year = int(r.get("Season_End_Year"))
            if end_year not in want_end_years:
                continue
            comp = str(r.get("Comp", "")).strip()
            league = FBREF_COMP_TO_KEY.get(comp)
            if not league:
                continue

            name = str(r.get("Player", "")).strip()
            team = str(r.get("Squad", "")).strip()
            pos = str(r.get("Pos", "")).strip()
            minutes = float(r.get("Min_Playing") or 0)
            games = float(r.get("MP_Playing") or 0)
            goals = float(r.get("Gls") or 0)
            npg = float(r.get("G_minus_PK") if r.get("G_minus_PK") is not None else goals)
            season = end_year - 1
        except Exception:
            continue

        if not name or games <= 0 or minutes < min_minutes:
            continue

        rows.append(
            {
                "player_name": name,
                "minutes": minutes,
                "games": games,
                "goals": goals,
                "npg": npg,
                "position": pos,
                "team_title": team,
                "league": league,
                "season": season,
                "source": "fbref",
            }
        )
    return rows


def fetch_openfootball_team_index(session: requests.Session, seasons: List[int]) -> Dict[Tuple[int, str, str], float]:
    # returns {(season, league, norm_team): goals_per_game_index}
    team_gpg: Dict[Tuple[int, str, str], List[float]] = defaultdict(list)

    for season in seasons:
        tag = f"{season}-{(season + 1) % 100:02d}"
        for league, code in OPENFOOTBALL_LEAGUE_FILES.items():
            url = f"https://raw.githubusercontent.com/openfootball/football.json/master/{tag}/{code}.json"
            resp = session.get(url, timeout=30)
            if resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except Exception:
                continue

            goals_for: Dict[str, float] = defaultdict(float)
            games: Dict[str, int] = defaultdict(int)
            for m in payload.get("matches", []):
                score = m.get("score") or {}
                ft = score.get("ft") if isinstance(score, dict) else None
                if not isinstance(ft, list) or len(ft) != 2:
                    continue
                t1 = normalize_team(m.get("team1", ""))
                t2 = normalize_team(m.get("team2", ""))
                if not t1 or not t2:
                    continue
                try:
                    g1 = float(ft[0])
                    g2 = float(ft[1])
                except Exception:
                    continue
                goals_for[t1] += g1
                goals_for[t2] += g2
                games[t1] += 1
                games[t2] += 1

            raw = {t: (goals_for[t] / games[t]) for t in games if games[t] > 0}
            if not raw:
                continue
            league_avg = sum(raw.values()) / len(raw)
            if league_avg <= 0:
                continue
            for t, gpg in raw.items():
                team_gpg[(season, league, t)].append(gpg / league_avg)

    return {k: sum(v) / len(v) for k, v in team_gpg.items() if v}


def fetch_football_data_team_index(session: requests.Session, seasons: List[int]) -> Dict[Tuple[int, str, str], float]:
    team_gpg: Dict[Tuple[int, str, str], List[float]] = defaultdict(list)

    for season in seasons:
        yy1 = season % 100
        yy2 = (season + 1) % 100
        season_code = f"{yy1:02d}{yy2:02d}"

        for league, code in FOOTBALL_DATA_CODES.items():
            url = f"https://www.football-data.co.uk/mmz4281/{season_code}/{code}.csv"
            resp = session.get(url, timeout=30)
            if resp.status_code != 200 or not resp.text:
                continue

            reader = csv.DictReader(io.StringIO(resp.text))
            goals_for: Dict[str, float] = defaultdict(float)
            games: Dict[str, int] = defaultdict(int)
            for row in reader:
                ht = normalize_team(row.get("HomeTeam", ""))
                at = normalize_team(row.get("AwayTeam", ""))
                if not ht or not at:
                    continue
                try:
                    hg = float(row.get("FTHG") or 0)
                    ag = float(row.get("FTAG") or 0)
                except Exception:
                    continue
                goals_for[ht] += hg
                goals_for[at] += ag
                games[ht] += 1
                games[at] += 1

            raw = {t: (goals_for[t] / games[t]) for t in games if games[t] > 0}
            if not raw:
                continue
            league_avg = sum(raw.values()) / len(raw)
            if league_avg <= 0:
                continue
            for t, gpg in raw.items():
                team_gpg[(season, league, t)].append(gpg / league_avg)

    return {k: sum(v) / len(v) for k, v in team_gpg.items() if v}


def get_team_attack_index(
    season: int,
    league: str,
    team: str,
    openfootball_idx: Dict[Tuple[int, str, str], float],
    football_data_idx: Dict[Tuple[int, str, str], float],
) -> Tuple[float, List[str]]:
    key = (season, league, normalize_team(team))
    vals: List[float] = []
    srcs: List[str] = []

    v1 = openfootball_idx.get(key)
    if v1 is not None:
        vals.append(v1)
        srcs.append("openfootball")

    v2 = football_data_idx.get(key)
    if v2 is not None:
        vals.append(v2)
        srcs.append("football-data")

    if not vals:
        return 1.0, []

    # clamp to keep multiplier sane
    blended = max(0.75, min(1.30, sum(vals) / len(vals)))
    return blended, srcs


def main() -> int:
    ap = argparse.ArgumentParser(description="Build multi-source player dataset for WCPGV")
    ap.add_argument(
        "--seasons",
        default="2024,2023,2022",
        help="Comma-separated recent seasons, newest first",
    )
    ap.add_argument("--min-minutes", type=int, default=300)
    ap.add_argument(
        "--source-weights",
        default="understat:1.0,fbref:0.9",
        help="Weights for player-data source blend",
    )
    ap.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent.parent / "data" / "multi_source_players_recent_top5.csv"),
    )
    args = ap.parse_args()

    seasons = parse_seasons(args.seasons)
    season_weights = recency_weights(seasons)
    src_weights = source_weights(args.source_weights)

    out_path = Path(args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    session = requests.Session()

    # 1) Understat player rows
    player_rows: List[dict] = []
    understat_rows = 0
    for season in seasons:
        for league in LEAGUES_UNDERSTAT:
            players = fetch_understat_league_players(session, league, season)
            for p in players:
                try:
                    name = str(p.get("player_name", "")).strip()
                    minutes = float(p.get("time", 0) or 0)
                    games = float(p.get("games", 0) or 0)
                    goals = float(p.get("goals", 0) or 0)
                    npg = float(p.get("npg", goals) or goals)
                    pos = str(p.get("position", "")).strip()
                    team = str(p.get("team_title", "")).strip()
                except Exception:
                    continue
                if not name or games <= 0 or minutes < args.min_minutes:
                    continue
                player_rows.append(
                    {
                        "player_name": name,
                        "minutes": minutes,
                        "games": games,
                        "goals": goals,
                        "npg": npg,
                        "position": pos,
                        "team_title": team,
                        "league": league,
                        "season": season,
                        "source": "understat",
                    }
                )
                understat_rows += 1

    # 2) FBref player rows
    fbref_rows_list = fetch_fbref_player_rows(session, seasons=seasons, min_minutes=args.min_minutes)
    fbref_rows = len(fbref_rows_list)
    player_rows.extend(fbref_rows_list)

    # 3/4) Team attack context from openfootball + football-data
    openfootball_idx = fetch_openfootball_team_index(session, seasons=seasons)
    football_data_idx = fetch_football_data_team_index(session, seasons=seasons)

    # Aggregate by normalized player
    agg: Dict[str, dict] = defaultdict(
        lambda: {
            "player_name": "",
            "position": "",
            "team_title": "",
            "league": "",
            "minutes": 0.0,
            "games": 0.0,
            "goals": 0.0,
            "npg": 0.0,
            "w_total": 0.0,
            "latest_minutes": -1.0,
            "latest_season": -1,
            "sources": set(),
            "team_attack_index": 1.0,
        }
    )

    for row in player_rows:
        season = int(row["season"])
        src = str(row["source"])
        weight = season_weights.get(season, 0.3) * src_weights.get(src, 1.0)
        if weight <= 0:
            continue

        key = normalize_name(row["player_name"])
        a = agg[key]
        a["minutes"] += float(row["minutes"]) * weight
        a["games"] += float(row["games"]) * weight
        a["goals"] += float(row["goals"]) * weight
        a["npg"] += float(row["npg"]) * weight
        a["w_total"] += weight
        a["sources"].add(src)

        if season > a["latest_season"] or (season == a["latest_season"] and float(row["minutes"]) > a["latest_minutes"]):
            a["latest_season"] = season
            a["latest_minutes"] = float(row["minutes"])
            a["player_name"] = row["player_name"]
            a["position"] = row["position"]
            a["team_title"] = row["team_title"]
            a["league"] = row["league"]

    output_rows = []
    attached_openfootball = 0
    attached_football_data = 0
    for _, a in agg.items():
        if a["w_total"] <= 0:
            continue

        team_attack_index, team_srcs = get_team_attack_index(
            season=a["latest_season"],
            league=a["league"],
            team=a["team_title"],
            openfootball_idx=openfootball_idx,
            football_data_idx=football_data_idx,
        )
        if "openfootball" in team_srcs:
            attached_openfootball += 1
        if "football-data" in team_srcs:
            attached_football_data += 1

        srcs = set(a["sources"])
        srcs.update(team_srcs)

        output_rows.append(
            {
                "player_name": a["player_name"],
                "minutes": int(round(a["minutes"] / a["w_total"])),
                "games": int(round(a["games"] / a["w_total"])),
                "goals": round(a["goals"] / a["w_total"], 3),
                "npg": round(a["npg"] / a["w_total"], 3),
                "position": a["position"],
                "team_title": a["team_title"],
                "league": a["league"],
                "season": ",".join(str(s) for s in seasons),
                "source": "+".join(sorted(srcs)),
                "team_attack_index": round(team_attack_index, 4),
            }
        )

    output_rows.sort(key=lambda r: (r["league"], -int(r["minutes"]), r["player_name"]))

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
        "team_attack_index",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(output_rows)

    print(
        f"Wrote {len(output_rows)} players to {out_path} | "
        f"understat_rows={understat_rows} fbref_rows={fbref_rows} "
        f"openfootball_team_idx={len(openfootball_idx)} football_data_team_idx={len(football_data_idx)} "
        f"players_with_openfootball={attached_openfootball} players_with_football_data={attached_football_data}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
