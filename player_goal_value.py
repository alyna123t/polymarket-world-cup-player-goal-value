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
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

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
}

_config = load_config(CONFIG_SCHEMA, __file__, slug="polymarket-world-cup-player-goal-value")

SKILL_SLUG = "polymarket-world-cup-player-goal-value"
TRADE_SOURCE = "sdk:world-cup-player-goal-value"
BASE = Path(__file__).parent
SPEND_PATH = BASE / "daily_spend.json"
COOLDOWN_PATH = BASE / "cooldown_state.json"


PLAYER_PRIORS: Dict[str, Dict[str, float]] = {
    # 0..1 factors
    "lionel messi": {"penalties": 1.0, "expected_matches": 0.90, "minutes": 0.62, "role": 0.86, "mismatch": 0.72},
    "ousmane dembele": {"penalties": 0.15, "expected_matches": 0.90, "minutes": 0.76, "role": 0.74, "mismatch": 0.68},
    "christian pulisic": {"penalties": 0.92, "expected_matches": 0.58, "minutes": 0.86, "role": 0.78, "mismatch": 0.62},
    "sadio mane": {"penalties": 0.74, "expected_matches": 0.52, "minutes": 0.81, "role": 0.78, "mismatch": 0.56},
}

DEFAULT_PRIOR = {"penalties": 0.45, "expected_matches": 0.45, "minutes": 0.65, "role": 0.62, "mismatch": 0.50}
WEIGHTS = {"penalties": 0.30, "expected_matches": 0.25, "minutes": 0.20, "role": 0.17, "mismatch": 0.08}

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


def extract_player_name(question: str) -> str:
    q = question.strip()
    # common pattern: "Will Lionel Messi score at least one goal ..."
    m = re.match(r"\s*Will\s+(.+?)\s+score at least one goal", q, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip().lower()
    # fallback first chunk before "score"
    m = re.match(r"\s*Will\s+(.+?)\s+score", q, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip().lower()
    return q.lower()[:80]


def is_player_goal_market(question: str) -> bool:
    q = question.lower()
    return (
        "world cup" in q
        and "score at least one goal" in q
        and q.startswith("will ")
    )


def estimate_fair_yes(player: str) -> float:
    pri = PLAYER_PRIORS.get(player, DEFAULT_PRIOR)
    score = sum(pri[k] * WEIGHTS[k] for k in WEIGHTS)
    # map score [0,1] to fair probability corridor [0.12, 0.82]
    fair = 0.12 + (0.70 * score)
    return max(0.02, min(0.95, fair))


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


def get_yes_ask(ctx: dict, market_obj) -> float:
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
                return float(v)
        except Exception:
            pass

    for attr in ("yes_ask", "ask_yes", "best_ask_yes", "ask", "best_ask", "current_probability"):
        try:
            v = getattr(market_obj, attr, None)
            if v is not None:
                return float(v)
        except Exception:
            pass

    return 0.5


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

    markets = client.get_markets(
        status="active",
        import_source=str(_config["import_source"]),
        limit=int(_config["scan_limit"]),
    )

    cands = [m for m in markets if is_player_goal_market(m.question)]

    if not quiet:
        print("⚽ World Cup Player Goal Value")
        print(f"scanned={len(markets)} candidates={len(cands)}")

    placed = []
    run_spent = 0.0

    # rank by model fair value (final edge is computed against ask inside loop)
    scored = []
    for m in cands:
        player = extract_player_name(m.question)
        fair = estimate_fair_yes(player)
        scored.append((fair, player, m))

    scored.sort(key=lambda x: x[0], reverse=True)

    for fair, player, m in scored:
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
                res = client.trade(
                    market_id=mid,
                    side="yes",
                    amount=amt,
                    action="buy",
                    venue=venue,
                    order_type="GTC",
                    price=px,
                    reasoning=note,
                    source=TRADE_SOURCE,
                    skill_slug=SKILL_SLUG,
                    allow_rebuy=False,
                    signal_data={
                        "player": player,
                        "fair_yes": round(fair, 5),
                        "ask_yes": round(ask_yes, 5),
                        "edge": round(edge, 5),
                        "spread": None if spread is None else round(spread, 5),
                        "slippage_pct": round(slip, 5),
                        "entry_price": px,
                    },
                )
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
            print(f"- {p['player']} | ${p['amount']:.2f} @ {p['price']:.3f} | edge={p['edge']:.3f} | {p['order_id']}")
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
        live=args.live,
        venue=args.venue,
        quiet=args.quiet,
        positions_only=args.positions,
        use_safeguards=not args.no_safeguards,
    )


if __name__ == "__main__":
    raise SystemExit(main())
