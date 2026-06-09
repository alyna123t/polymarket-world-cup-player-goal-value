#!/usr/bin/env python3
"""
Polymarket World Cup Player Goal Value

Thesis (from user-shared article):
- In "player to score at least once" markets, edge comes from
  penalty duty, expected matches, role/minutes certainty, and
  mismatch game upside.
- Liquidity is often thin; use patient limit orders.

This skill scores player-goal YES markets and places conservative
limit buys only when model edge exceeds threshold.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

from simmer_sdk.skill import get_config_path, load_config, update_config

sys.stdout.reconfigure(line_buffering=True)


CONFIG_SCHEMA = {
    "scan_limit": {"env": "SIMMER_WCPGV_SCAN_LIMIT", "default": 400, "type": int, "help": "Markets to scan"},
    "import_source": {"env": "SIMMER_WCPGV_IMPORT_SOURCE", "default": "polymarket", "type": str, "help": "Source filter"},
    "min_edge": {"env": "SIMMER_WCPGV_MIN_EDGE", "default": 0.06, "type": float, "help": "Minimum fair-price edge"},
    "max_spread": {"env": "SIMMER_WCPGV_MAX_SPREAD", "default": 0.04, "type": float, "help": "Skip if spread above this"},
    "max_slippage_pct": {"env": "SIMMER_WCPGV_MAX_SLIPPAGE", "default": 0.05, "type": float, "help": "Skip if slippage above this"},
    "max_position_usd": {"env": "SIMMER_WCPGV_MAX_POSITION", "default": 12.0, "type": float, "help": "Max USD per market"},
    "daily_budget_usd": {"env": "SIMMER_WCPGV_DAILY_BUDGET", "default": 40.0, "type": float, "help": "Daily spend cap"},
    "max_trades_per_run": {"env": "SIMMER_WCPGV_MAX_TRADES", "default": 3, "type": int, "help": "Max orders per run"},
    "cooldown_hours": {"env": "SIMMER_WCPGV_COOLDOWN_H", "default": 24, "type": int, "help": "Per-market cooldown"},
    "limit_offsets_cents": {"env": "SIMMER_WCPGV_LIMIT_OFFSETS", "default": "8,5,3", "type": str, "help": "Entry ladder offsets from fair, cents"},
    "limit_splits": {"env": "SIMMER_WCPGV_LIMIT_SPLITS", "default": "0.25,0.35,0.40", "type": str, "help": "Allocation split per ladder rung"},
    "player_data_file": {"env": "SIMMER_WCPGV_PLAYER_DATA_FILE", "default": "data/understat_players_recent_top5.csv", "type": str, "help": "CSV of real player stats"},
    "min_player_minutes": {"env": "SIMMER_WCPGV_MIN_PLAYER_MINUTES", "default": 450, "type": int, "help": "Skip players below this season-minute sample"},
    "expected_tournament_matches": {"env": "SIMMER_WCPGV_EXPECTED_MATCHES", "default": 3.4, "type": float, "help": "Expected matches for tournament-style markets (e.g., World Cup)"},
    "expected_single_market_matches": {"env": "SIMMER_WCPGV_EXPECTED_SINGLE_MATCHES", "default": 1.0, "type": float, "help": "Expected matches for single-game scoring markets"},
    "expected_season_market_matches": {"env": "SIMMER_WCPGV_EXPECTED_SEASON_MATCHES", "default": 8.0, "type": float, "help": "Expected remaining matches for season-long scoring props"},
    "allow_proxy_price_in_sim_only": {"env": "SIMMER_WCPGV_ALLOW_PROXY_SIM", "default": True, "type": bool, "help": "When no ask quote is available, allow current-probability proxy pricing in sim venue only"},
}

_config = load_config(CONFIG_SCHEMA, __file__, slug="polymarket-world-cup-player-goal-value")

SKILL_SLUG = "polymarket-world-cup-player-goal-value"
TRADE_SOURCE = "sdk:world-cup-player-goal-value"
BASE = Path(__file__).parent
SPEND_PATH = BASE / "daily_spend.json"
COOLDOWN_PATH = BASE / "cooldown_state.json"


ROLE_MULTIPLIERS = {
    "F": 1.20,  # forwards/strikers
    "M": 0.85,  # midfielders
    "D": 0.45,  # defenders
    "G": 0.05,  # goalkeepers
}

PLAYER_DATA_CACHE: Optional[Dict[str, dict]] = None

_client = None
VENUE_CHOICES = ("sim", "polymarket", "kalshi")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


def load_daily_spend() -> dict:
    d = now_utc().strftime("%Y-%m-%d")
    data = load_json(SPEND_PATH, {"date": d, "spent": 0.0, "trades": 0})
    if data.get("date") != d:
        data = {"date": d, "spent": 0.0, "trades": 0}
    return data


def get_client(live: bool, venue: str):
    global _client
    if _client is None:
        from simmer_sdk import SimmerClient

        key = os.environ.get("SIMMER_API_KEY")
        if not key:
            print("Error: SIMMER_API_KEY not set")
            sys.exit(1)
        _client = SimmerClient(api_key=key, venue=venue, live=live)
    return _client


def get_positions(client, venue: str) -> List[dict]:
    try:
        from dataclasses import asdict

        positions = client.get_positions(venue=venue)
        return [asdict(p) for p in positions]
    except Exception as e:
        print(f"Error fetching positions: {e}")
        return []


def api_market_search(query: str, limit: int) -> List[SimpleNamespace]:
    """Direct API search for player-goal markets absent from snapshot feed."""
    key = os.environ.get("SIMMER_API_KEY")
    if not key:
        return []

    params = urllib.parse.urlencode(
        {
            "q": query,
            "status": "active",
            "venue": "polymarket",
            "limit": max(1, min(limit, 1000)),
        }
    )
    url = f"https://api.simmer.markets/api/sdk/markets?{params}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return []

    out: List[SimpleNamespace] = []
    for m in (data.get("markets") or []):
        if not isinstance(m, dict):
            continue
        out.append(
            SimpleNamespace(
                id=m.get("id"),
                question=m.get("question", ""),
                spread=m.get("spread"),
                current_probability=m.get("current_probability"),
            )
        )
    return out


def discover_markets(client) -> List:
    base_markets = client.get_markets(
        status="active",
        import_source=str(_config["import_source"]),
        limit=int(_config["scan_limit"]),
    )

    queries = [
        "score a goal at the 2026 FIFA World Cup",
        "World Cup: Player to score",
        "to Score 2+ Penalties",
    ]

    extra: List[SimpleNamespace] = []
    for q in queries:
        extra.extend(api_market_search(q, limit=int(_config["scan_limit"])))

    merged = []
    seen = set()
    for m in list(base_markets) + extra:
        mid = getattr(m, "id", None)
        q = getattr(m, "question", "")
        if not mid or not q or mid in seen:
            continue
        seen.add(mid)
        merged.append(m)
    return merged


def check_context_safeguards(context: dict):
    if not context:
        return True, []

    reasons = []
    warnings = context.get("warnings", [])
    discipline = context.get("discipline", {})

    for warning in warnings:
        if "MARKET RESOLVED" in str(warning).upper():
            return False, ["Market already resolved"]

    warning_level = discipline.get("warning_level", "none")
    if warning_level == "severe":
        return False, [f"Severe flip-flop warning: {discipline.get('flip_flop_warning', '')}"]
    if warning_level == "mild":
        reasons.append("Mild flip-flop warning (proceed with caution)")

    return True, reasons


def parse_csv_floats(s: str) -> List[float]:
    vals = []
    for x in s.split(","):
        x = x.strip()
        if not x:
            continue
        vals.append(float(x))
    return vals


def normalize_player_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-zA-Z0-9\s\-']", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def loose_name_key(name: str) -> str:
    s = normalize_player_name(name)
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def lookup_player_data(player: str, data: Dict[str, dict]) -> Optional[dict]:
    exact = normalize_player_name(player)
    if exact in data:
        return data[exact]

    target = loose_name_key(player)
    target_tokens = [t for t in target.split() if t]
    if not target_tokens:
        return None

    candidates: List[tuple[int, dict]] = []
    for key, row in data.items():
        lk = loose_name_key(key)
        if target == lk:
            return row
        k_tokens = set(lk.split())
        overlap = sum(1 for t in target_tokens if t in k_tokens)
        if overlap == len(target_tokens):
            candidates.append((overlap, row))

    if len(candidates) == 1:
        return candidates[0][1]
    return None


def extract_player_name(question: str) -> str:
    q = question.strip()
    patterns = [
        r"\s*Will\s+(.+?)\s+score at least one goal",
        r"\s*Will\s+(.+?)\s+score\b",
        r"\s*Will\s+(.+?)\s+have\s+\d+\+?\s+goals?",
        r"\s*Will\s+(.+?)\s+to\s+score\b",
    ]
    for pat in patterns:
        m = re.match(pat, q, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return q[:80]


def is_player_goal_market(question: str) -> bool:
    q = question.strip().lower()
    if not q.startswith("will "):
        return False

    if "score" in q and any(x in q for x in ["goal", "against", "this season", "in the match", "in this match"]):
        return True
    if re.search(r"\bhave\s+\d+\+?\s+goals?\b", q):
        return True
    if "to score" in q:
        return True
    return False


def infer_expected_matches(question: str) -> float:
    q = question.lower()
    if "world cup" in q or "euro" in q or "tournament" in q:
        return float(_config["expected_tournament_matches"])
    if "this season" in q or "season" in q:
        return float(_config["expected_season_market_matches"])
    return float(_config["expected_single_market_matches"])


def get_player_data_path() -> Path:
    return (BASE / str(_config["player_data_file"])).resolve()


def role_multiplier(position: str) -> float:
    tokens = [t.strip().upper() for t in str(position).replace("/", " ").split() if t.strip()]
    vals = [ROLE_MULTIPLIERS[t[0]] for t in tokens if t and t[0] in ROLE_MULTIPLIERS]
    if not vals:
        return 0.80
    return max(vals)


def load_player_data() -> Dict[str, dict]:
    global PLAYER_DATA_CACHE
    if PLAYER_DATA_CACHE is not None:
        return PLAYER_DATA_CACHE

    path = get_player_data_path()
    if not path.exists():
        raise FileNotFoundError(f"Player data CSV not found: {path}")

    out: Dict[str, dict] = {}
    min_minutes = int(_config["min_player_minutes"])
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                name = str(row.get("player_name", "")).strip()
                if not name:
                    continue
                minutes = float(row.get("minutes", 0) or 0)
                goals = float(row.get("goals", 0) or 0)
                games = float(row.get("games", 0) or 0)
                npg = float(row.get("npg", goals) or goals)
                position = str(row.get("position", "")).strip()
            except Exception:
                continue

            if minutes < min_minutes or games <= 0:
                continue

            goals_per90 = (goals * 90.0 / minutes) if minutes > 0 else 0.0
            npg_per90 = (npg * 90.0 / minutes) if minutes > 0 else goals_per90
            exp_minutes = max(20.0, min(90.0, minutes / games))
            pk_goals = max(0.0, goals - npg)
            pk_share = (pk_goals / goals) if goals > 0 else 0.0
            team_attack_index = float(row.get("team_attack_index", 1.0) or 1.0)
            team_attack_index = max(0.75, min(1.30, team_attack_index))

            norm = normalize_player_name(name)
            out[norm] = {
                "player_name": name,
                "minutes": minutes,
                "games": games,
                "position": position,
                "goals": goals,
                "npg": npg,
                "goals_per90": goals_per90,
                "npg_per90": npg_per90,
                "expected_minutes": exp_minutes,
                "role_multiplier": role_multiplier(position),
                "penalty_goal_share": pk_share,
                "team_attack_index": team_attack_index,
                "source": row.get("source", "understat"),
                "season": row.get("season", ""),
                "league": row.get("league", ""),
                "team": row.get("team_title", ""),
            }

    PLAYER_DATA_CACHE = out
    return out


def estimate_fair_yes(player: str, pdata: dict, question: str) -> Tuple[float, dict]:
    g90 = float(pdata["goals_per90"])
    exp_minutes = float(pdata["expected_minutes"])
    role_mult = float(pdata["role_multiplier"])
    team_attack_index = float(pdata.get("team_attack_index", 1.0) or 1.0)
    expected_matches = infer_expected_matches(question)

    # Non-penalty scoring base, then small add-on for known penalty contribution.
    base_lambda = g90 * (exp_minutes / 90.0) * expected_matches
    pk_uplift = 1.0 + (0.18 * float(pdata["penalty_goal_share"]))
    lam = max(0.0, base_lambda * role_mult * pk_uplift * team_attack_index)

    fair = 1.0 - math.exp(-lam)
    fair = max(0.01, min(0.98, fair))
    return fair, {
        "lambda": lam,
        "goals_per90": g90,
        "expected_minutes": exp_minutes,
        "role_multiplier": role_mult,
        "penalty_goal_share": float(pdata["penalty_goal_share"]),
        "expected_market_matches": expected_matches,
        "team_attack_index": team_attack_index,
    }


def max_slippage_pct(ctx: dict) -> float:
    est = (ctx.get("slippage") or {}).get("estimates") or []
    vals = []
    for e in est:
        try:
            vals.append(float(e.get("slippage_pct", 0.0)))
        except Exception:
            pass
    return max(vals) if vals else 0.0


def safe_spread(ctx: dict, market_obj) -> Optional[float]:
    m = (ctx or {}).get("market") or {}
    for key in ("spread",):
        try:
            v = m.get(key, None)
            if v is not None:
                return float(v)
        except Exception:
            pass
    try:
        v = getattr(market_obj, "spread", None)
        if v is not None:
            return float(v)
    except Exception:
        pass
    return None


def get_yes_ask(ctx: dict, market_obj) -> Optional[float]:
    m = (ctx or {}).get("market") or {}
    keys = (
        "yes_ask",
        "ask_yes",
        "best_ask_yes",
        "ask",
        "best_ask",
    )
    for key in keys:
        try:
            v = m.get(key, None)
            if v is not None:
                fv = float(v)
                if 0.0 < fv < 1.0:
                    return fv
        except Exception:
            pass

    for attr in ("yes_ask", "ask_yes", "best_ask_yes", "ask", "best_ask"):
        try:
            v = getattr(market_obj, attr, None)
            if v is not None:
                fv = float(v)
                if 0.0 < fv < 1.0:
                    return fv
        except Exception:
            pass

    return None


def get_proxy_yes_price(ctx: dict, market_obj) -> Optional[float]:
    m = (ctx or {}).get("market") or {}
    for key in ("current_probability", "current_price", "probability", "price_yes"):
        try:
            v = m.get(key, None)
            if v is not None:
                fv = float(v)
                if 0.0 < fv < 1.0:
                    return fv
        except Exception:
            pass

    for attr in ("current_probability", "current_price", "probability", "price_yes"):
        try:
            v = getattr(market_obj, attr, None)
            if v is not None:
                fv = float(v)
                if 0.0 < fv < 1.0:
                    return fv
        except Exception:
            pass

    return None


def run(
    live: bool,
    venue: str,
    quiet: bool = False,
    positions_only: bool = False,
    use_safeguards: bool = True,
) -> int:
    client = get_client(live, venue)

    if positions_only:
        print(json.dumps(get_positions(client, venue), indent=2))
        return 0

    spend = load_daily_spend()
    cooldown = load_json(COOLDOWN_PATH, {})
    tnow = now_utc().timestamp()

    offsets = parse_csv_floats(str(_config["limit_offsets_cents"]))
    splits = parse_csv_floats(str(_config["limit_splits"]))
    if len(offsets) != len(splits) or abs(sum(splits) - 1.0) > 1e-6:
        print("Invalid ladder config: offsets/splits mismatch or splits not summing to 1.0")
        return 2

    markets = discover_markets(client)

    cands = [m for m in markets if is_player_goal_market(m.question)]

    if not quiet:
        print("⚽ Player Goal Value")
        print(f"scanned={len(markets)} candidates={len(cands)}")

    try:
        player_data = load_player_data()
    except Exception as e:
        print(f"Error loading player data: {e}")
        return 2

    placed = []
    run_spent = 0.0
    skipped_unknown = 0

    # rank by model fair value (final edge is computed against ask inside loop)
    scored = []
    for m in cands:
        player = extract_player_name(m.question)
        pdata = lookup_player_data(player, player_data)
        if not pdata:
            skipped_unknown += 1
            continue
        fair, model_inputs = estimate_fair_yes(player, pdata, m.question)
        scored.append((fair, player, m, pdata, model_inputs))

    scored.sort(key=lambda x: x[0], reverse=True)

    if not quiet:
        print(f"known_players={len(scored)} skipped_unknown={skipped_unknown}")

    for fair, player, m, pdata, model_inputs in scored:
        if len(placed) >= int(_config["max_trades_per_run"]):
            break

        if spend["spent"] + run_spent >= float(_config["daily_budget_usd"]):
            break

        mid = m.id
        last = float(cooldown.get(mid, 0.0))
        if tnow - last < float(_config["cooldown_hours"]) * 3600:
            continue

        ctx = client.get_market_context(mid, venue=venue) or {}
        ask_yes = get_yes_ask(ctx, m)
        if ask_yes is None:
            allow_proxy = bool(_config.get("allow_proxy_price_in_sim_only", True)) and venue == "sim"
            if allow_proxy:
                ask_yes = get_proxy_yes_price(ctx, m)
                if ask_yes is not None:
                    if not quiet:
                        print(f"proxy-ask-used(sim): {m.question[:72]}... ask≈{ask_yes:.3f}")
            if ask_yes is None:
                if not quiet:
                    print(f"skip-no-ask: {m.question[:72]}...")
                continue
        edge = fair - ask_yes
        if edge < float(_config["min_edge"]):
            continue

        if use_safeguards:
            should_trade, reasons = check_context_safeguards(ctx)
            if not should_trade:
                continue
            if reasons and not quiet:
                print(f"safeguard: {m.question[:64]}... -> {'; '.join(reasons)}")
        spread = safe_spread(ctx, m)
        slip = max_slippage_pct(ctx)

        if spread is not None and spread > float(_config["max_spread"]):
            continue
        if slip > float(_config["max_slippage_pct"]):
            continue

        total = float(_config["max_position_usd"])
        rung_orders = []
        for off_c, split in zip(offsets, splits):
            px = max(0.001, min(0.999, fair - (off_c / 100.0)))
            amt = round(total * split, 2)
            if amt < 1.0:
                continue
            rung_orders.append((px, amt, off_c))

        # place GTC limits (patient fill)
        any_ok = False
        for px, amt, off_c in rung_orders:
            if spend["spent"] + run_spent + amt > float(_config["daily_budget_usd"]):
                continue
            note = (
                f"WCPGV edge={edge:.3f} fair={fair:.3f} px={px:.3f} off={off_c:.1f}c "
                f"player={player}"
            )

            if live:
                signal_data = {
                    "player": player,
                    "fair_yes": round(fair, 5),
                    "ask_yes": round(ask_yes, 5),
                    "edge": round(edge, 5),
                    "spread": None if spread is None else round(spread, 5),
                    "slippage_pct": round(slip, 5),
                    "entry_price": px,
                    "goals_per90": round(float(model_inputs["goals_per90"]), 5),
                    "expected_minutes": round(float(model_inputs["expected_minutes"]), 2),
                    "role_multiplier": round(float(model_inputs["role_multiplier"]), 5),
                    "penalty_goal_share": round(float(model_inputs["penalty_goal_share"]), 5),
                    "expected_market_matches": round(float(model_inputs["expected_market_matches"]), 3),
                    "team_attack_index": round(float(model_inputs["team_attack_index"]), 5),
                    "lambda": round(float(model_inputs["lambda"]), 5),
                    "player_source": pdata.get("source", "understat"),
                    "player_league": pdata.get("league", ""),
                    "player_team": pdata.get("team", ""),
                }

                trade_kwargs = {
                    "market_id": mid,
                    "side": "yes",
                    "amount": amt,
                    "action": "buy",
                    "venue": venue,
                    "reasoning": note,
                    "source": TRADE_SOURCE,
                    "skill_slug": SKILL_SLUG,
                    "allow_rebuy": False,
                    "signal_data": signal_data,
                }

                # Sim/Kalshi venues don't accept explicit price in SDK trade().
                # Keep GTC/price only for Polymarket.
                if venue == "polymarket":
                    trade_kwargs["order_type"] = "GTC"
                    trade_kwargs["price"] = px

                res = client.trade(**trade_kwargs)
                ok = bool(getattr(res, "success", False))
                oid = getattr(res, "order_id", None)
            else:
                ok = True
                oid = "dry-run"

            if ok:
                any_ok = True
                run_spent += amt
                placed.append({
                    "player": player,
                    "question": m.question,
                    "edge": round(edge, 4),
                    "fair": round(fair, 4),
                    "price": px,
                    "amount": amt,
                    "order_id": oid,
                    "goals_per90": round(float(model_inputs["goals_per90"]), 3),
                    "exp_minutes": round(float(model_inputs["expected_minutes"]), 1),
                    "role_mult": round(float(model_inputs["role_multiplier"]), 3),
                    "team_attack_idx": round(float(model_inputs["team_attack_index"]), 3),
                })

        if any_ok:
            cooldown[mid] = tnow

    if live:
        spend["spent"] = round(float(spend["spent"]) + run_spent, 2)
        spend["trades"] = int(spend.get("trades", 0)) + len(placed)
        save_json(SPEND_PATH, spend)
        save_json(COOLDOWN_PATH, cooldown)

    if placed:
        print(f"Placed {len(placed)} limit entries")
        for p in placed:
            print(
                f"- {p['player']} | ${p['amount']:.2f} @ {p['price']:.3f} | edge={p['edge']:.3f} "
                f"| g90={p['goals_per90']:.3f} min={p['exp_minutes']:.1f} role={p['role_mult']:.2f} "
                f"team={p['team_attack_idx']:.2f} | {p['order_id']}"
            )
    else:
        print("No eligible value entries this run.")

    print(f"Daily spent: ${spend['spent']:.2f} / ${float(_config['daily_budget_usd']):.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="World Cup player-goal value trader")
    ap.add_argument("--live", action="store_true", help="Place real orders")
    ap.add_argument("--venue", choices=VENUE_CHOICES, default="polymarket", help="Trading venue")
    ap.add_argument("--positions", action="store_true", help="Show current positions and exit")
    ap.add_argument("--no-safeguards", action="store_true", help="Disable context safeguards")
    ap.add_argument("--quiet", action="store_true", help="Quiet output")
    ap.add_argument("--config", action="store_true", help="Print current config")
    ap.add_argument("--set", action="append", default=[], help="Update config key=value")
    args = ap.parse_args()

    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print(f"Invalid --set: {item}")
                return 2
            k, v = item.split("=", 1)
            k = k.strip()
            if k not in CONFIG_SCHEMA:
                print(f"Unknown config key: {k}")
                return 2
            t = CONFIG_SCHEMA[k]["type"]
            try:
                updates[k] = t(v)
            except Exception as e:
                print(f"Failed parse {k}: {e}")
                return 2
        update_config(updates, __file__)
        print(f"Updated config at {get_config_path(__file__)}")
        return 0

    if args.config:
        print(json.dumps(_config, indent=2))
        return 0

    return run(
        # positions-only mode should query the same account context as live reads,
        # otherwise `--positions` can look empty after successful live sim orders.
        live=(args.live or args.positions),
        venue=args.venue,
        quiet=args.quiet,
        positions_only=args.positions,
        use_safeguards=not args.no_safeguards,
    )


if __name__ == "__main__":
    raise SystemExit(main())
