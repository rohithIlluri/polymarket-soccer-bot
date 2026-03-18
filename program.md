# Soccer Betting Bot — Agent Instructions

## Objective
Maximize risk-adjusted P&L (Sharpe ratio, annualized on daily P&L) on Polymarket
soccer betting markets over a rolling 30-day window.
Secondary: positive cumulative P&L. Tertiary: win_rate > 0.50.

## What You Control
You may ONLY modify `trade.py`. Never touch `market_data.py`, `run.py`, or `program.md`.

## Editable Items in trade.py

### Parameters (bounds strictly enforced here)
| Parameter | Min | Max | Notes |
|---|---|---|---|
| EDGE_THRESHOLD | 0.02 | 0.20 | Lower = more bets, higher = more selective |
| KELLY_FRACTION | 0.05 | 0.50 | Never exceed 0.50 — ruin risk |
| MIN_MARKET_PRICE | 0.05 | 0.40 | Left bound of YES price range |
| MAX_MARKET_PRICE | 0.60 | 0.95 | Right bound of YES price range |
| MAX_CONCURRENT_BETS | 1 | 5 | Concurrent open positions |
| BOOKMAKER_WEIGHT | 0.0 | 1.0 | Weights must sum to 1.0 |
| XG_WEIGHT | 0.0 | 1.0 | |
| FORM_WEIGHT | 0.0 | 1.0 | |
| MIN_DATA_QUALITY | "none"\|"partial"\|"full" | | |
| MIN_LIQUIDITY_HARD | 500 | 50000 | USD minimum per market |

### Functions you may improve
- `estimate_probability(ctx)` — the core prediction model
- `evaluate_markets(contexts, balance, max_bet)` — market selection logic
- `handle_live_event(event, market, balance)` — in-play reaction (currently no-op)

## Data Available in MatchContext (ctx)
```
ctx.market.yes_price          float   Polymarket implied YES probability
ctx.market.no_price           float   Polymarket implied NO probability
ctx.market.liquidity_usd      float   Market depth in USDC
ctx.bookmaker_home_prob       float   Bookmaker consensus HOME win prob (0–1)
ctx.bookmaker_away_prob       float   Bookmaker consensus AWAY win prob (0–1)
ctx.home_xg_avg               float   Home team avg xG last 5 matches
ctx.away_xg_avg               float   Away team avg xG last 5 matches
ctx.home_form                 list    e.g. ["W","D","L","W","W"]
ctx.away_form                 list    e.g. ["L","W","W","D","W"]
ctx.h2h_home_wins             int     Head-to-head home wins (last 10)
ctx.h2h_away_wins             int     Head-to-head away wins (last 10)
ctx.h2h_draws                 int     Head-to-head draws (last 10)
ctx.data_quality              str     "full" | "partial" | "none"
```

## Improvement Heuristics

**If n_bets == 0 consistently (no bets placed):**
→ EDGE_THRESHOLD is too high; lower it by 0.01–0.02
→ Or MIN_DATA_QUALITY too strict; try "none"

**If win_rate < 0.40 and n_bets > 5:**
→ Model is wrong; try increasing BOOKMAKER_WEIGHT toward 0.70
→ Tighten EDGE_THRESHOLD by +0.02
→ Reduce KELLY_FRACTION by 0.05

**If win_rate > 0.55 and Sharpe > 0.5:**
→ Carefully loosen EDGE_THRESHOLD by -0.01
→ Slightly increase KELLY_FRACTION by 0.05

**If P&L negative but win_rate > 0.50:**
→ Bet sizing issue — reduce KELLY_FRACTION
→ May be betting on low-value YES/NO sides; tighten price bounds

**If Sharpe > 1.0:**
→ Strategy is working; make only minor adjustments

**Advanced improvements:**
→ Use h2h data in estimate_probability (home advantage effect)
→ Detect when bookmaker_home_prob >> market.yes_price (bookmaker sees value the market misses)
→ Weight form more heavily for matches in top leagues (CL, PL, BL1)
→ Add competition-specific EDGE_THRESHOLD multiplier

## Hard Rules
1. KELLY_FRACTION must NEVER exceed 0.50
2. EDGE_THRESHOLD must NEVER go below 0.02
3. Weights BOOKMAKER_WEIGHT + XG_WEIGHT + FORM_WEIGHT must sum to 1.0
4. Do not add external imports (only stdlib + already-imported packages)
5. evaluate_markets must return list[dict] with keys: token_id, side, amount_usd, reason, market_id, question

## Output Format
Output ONLY the complete contents of trade.py starting with:
```
# Change: <one-line description of what changed and why, based on results>
```
No markdown code fences. No explanations outside the file.
