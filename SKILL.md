---
name: polymarket-world-cup-player-goal-value
description: Trade FIFA World Cup “player to score at least once” markets using role/minutes/penalty/deep-run value scoring and patient limit orders.
metadata:
  author: Alyna + Hermes
  version: "0.1.0"
  displayName: Polymarket World Cup Player Goal Value
  difficulty: intermediate
---

# Polymarket World Cup Player Goal Value

This skill implements a value framework for **World Cup player-goal YES markets**.

It models fair value using:
- penalty duty
- expected number of matches (deep-run probability)
- minutes certainty / starter status
- attacking role centrality
- mismatch game upside

Then it places **patient limit buy ladders** only when fair value edge exceeds a threshold.

Reference inspiration:
https://x.com/Predicti0r/status/2061791808158400570

## What it does

- Scans active World Cup player-goal markets on Polymarket imports
- Computes model fair YES probability per player
- Requires minimum edge (`fair - market_price >= min_edge`)
- Applies spread/slippage quality gates
- Places laddered limit orders (`GTC`) at discounted prices from fair
- Enforces cooldown + daily budget controls

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

python player_goal_value.py --config
python player_goal_value.py            # dry run
python player_goal_value.py --live     # live orders
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

- Uses conservative built-in priors for known players from the article and fallback priors for others.
- Designed for low-liquidity conditions where market chasing is penalized.
- Start in dry-run/sim and adjust priors after collecting your own outcomes.

## Deterministic spec (Skill Builder style)

### Signal
- Player fair-value estimate from weighted priors:
  - penalties, expected matches, minutes certainty, role centrality, mismatch upside

### Entry logic
- Require `fair_yes - market_yes >= min_edge`
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
