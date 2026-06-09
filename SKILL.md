---
name: polymarket-world-cup-player-goal-value
description: Trade Polymarket player-goal YES markets (World Cup + league + match props) using role/minutes/penalty/value scoring and patient limit orders.
metadata:
  author: Alyna + Hermes
  version: "0.1.6"
  displayName: Polymarket World Cup Player Goal Value
  difficulty: intermediate
---

# Polymarket Player Goal Value

This skill implements a value framework for **player-goal YES markets** (World Cup + league + match-level props) using real player-level scoring data.

It models fair value using:
- real historical goals/90
- expected minutes per appearance (from real minutes/games)
- role from position (F/M/D/G)
- small penalty-goal uplift (from goals vs non-penalty goals)

Then it places **patient limit buy ladders** only when fair value edge exceeds a threshold.

Reference inspiration:
https://x.com/Predicti0r/status/2061791808158400570

## What it does

- Scans active player-goal markets from Simmer imports
- Loads single-source player data from `data/understat_players_recent_top5.csv`
- Converts player stats into fair `P(score >= 1)` with a Poisson model
- **Skips unknown players** (no fallback guessing)
- Uses **ask price** for edge checks (`fair - ask_yes >= min_edge`)
- Applies spread/slippage quality gates + optional context safeguards
- Places laddered limit orders (`GTC`) at discounted prices from fair
- Enforces cooldown + daily budget controls
- Supports `--venue` (`sim`, `polymarket`, `kalshi`) for testing and deployment

## Defaults

- Dry-run by default (no real orders)
- `min_edge`: 0.06
- `max_position_usd`: 12
- `daily_budget_usd`: 40
- `max_trades_per_run`: 3
- limit ladder: 8c / 5c / 3c below fair with 25% / 35% / 40% allocation

## Run

```bash
cd skills/polymarket-world-cup-player-goal-value

# refresh player stats from the free Understat connector
python scripts/fetch_understat_players.py --seasons 2026,2025,2024 --min-minutes 300

python player_goal_value.py --config
python player_goal_value.py --venue sim                  # dry run in $SIM venue
python player_goal_value.py --venue sim --positions      # inspect open sim positions
python player_goal_value.py --venue polymarket --live    # live orders on Polymarket
```

## Tune

```bash
python player_goal_value.py --set min_edge=0.08
python player_goal_value.py --set max_position_usd=20
python player_goal_value.py --set daily_budget_usd=75
python player_goal_value.py --set limit_offsets_cents=10,6,4
python player_goal_value.py --set limit_splits=0.2,0.3,0.5
```

## Notes

- Uses free Understat player data (top 5 leagues) for goals, non-penalty goals, minutes, and role.
- Latest season in `--seasons` is included only if upstream sources already expose player rows (e.g. 2026 may be empty pre-kickoff).
- Unknown player markets are skipped instead of falling back to synthetic priors.
- Designed for low-liquidity conditions where market chasing is penalized.
- Start in dry-run/sim and tune `expected_tournament_matches`, `expected_single_market_matches`, `expected_season_market_matches`, `min_edge`, and minute filters after collecting outcomes.

## Bugfixes applied in v0.1.6

- Added fallback discovery search via `/api/sdk/markets?q=...` and merged + deduped results with snapshot feed to recover missing player-goal candidates.
- Added sim-only proxy quote option (`allow_proxy_price_in_sim_only`) so missing asks in `$SIM` no longer block all execution tests.
- Fixed venue-specific trade payload: only send `price` + `GTC` on `polymarket`; `sim/kalshi` now submit supported market orders.
- `--positions` now queries live account context so it reflects executed sim orders without requiring a separate `--live` flag.
- Existing v0.1.1 fixes retained: venue plumbing, dry-run state isolation, ask-based edge checks, SDK-managed price normalization.

## Deterministic spec (Skill Builder style)

### Signal
- Player fair-value estimate from weighted priors:
  - penalties, expected matches, minutes certainty, role centrality, mismatch upside

### Entry logic
- Require `fair_yes - ask_yes >= min_edge`
- Build discounted limit ladder below fair value
- Enter only when spread/slippage/cooldown/budget gates pass

### Exit logic
- v0.1 focuses on disciplined entry only
- Exit handling can be layered as explicit sell rules in future version

### Market selection
- Active Polymarket-imported World Cup “player to score at least one goal” markets

### Position sizing
- Fixed per-market cap `max_position_usd`
- Ladder split from `limit_splits`

### Risk controls
- `max_spread`, `max_slippage_pct`
- `cooldown_hours`
- `max_trades_per_run`
- `daily_budget_usd`
- optional context safeguards (disable with `--no-safeguards`)
