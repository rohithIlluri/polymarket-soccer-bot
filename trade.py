# Change: baseline strategy — bookmaker-weighted + xG blend + form signal
from typing import Optional

"""
trade.py — AGENT-EDITABLE strategy file. Mirrors train.py in autoresearch.

This is the ONLY file the agent modifies each iteration.
run.py commits it, runs it, and reverts if metrics don't improve.

HARD CONSTRAINTS (enforced externally — do not bypass):
- Max bet: $MAX_BET_USD per position
- Max daily loss: $MAX_DAILY_LOSS_USD circuit breaker
- Pre-match only (no in-play)
- Liquidity filter: $MIN_LIQUIDITY_USD

AGENT-TUNABLE PARAMETERS (adjust to improve Sharpe + P&L):
"""

# ── Strategy Parameters ────────────────────────────────────────────────────

# Minimum edge (est_prob - market_price) required to place a bet
EDGE_THRESHOLD = 0.06

# Kelly fraction (0.05–0.50). Lower = more conservative sizing
KELLY_FRACTION = 0.25

# Only bet on markets within this YES price range (avoids heavy favorites)
MIN_MARKET_PRICE = 0.15
MAX_MARKET_PRICE = 0.85

# Maximum simultaneous open bets
MAX_CONCURRENT_BETS = 3

# Probability model weights (must sum to 1.0)
BOOKMAKER_WEIGHT = 0.50   # consensus bookmaker probability
XG_WEIGHT        = 0.30   # xG-based signal
FORM_WEIGHT      = 0.20   # recent form signal

# Minimum data quality required to place a bet: "full" | "partial" | "none"
MIN_DATA_QUALITY = "partial"

# Skip markets whose liquidity is below this even if above env var minimum
MIN_LIQUIDITY_HARD = 1000.0


# ── Probability Model ──────────────────────────────────────────────────────
def estimate_probability(ctx) -> float:
    """
    Given a MatchContext, estimate the TRUE probability that YES resolves.
    Blends bookmaker consensus, xG signal, and recent form.

    ctx fields available:
        ctx.market.yes_price          float   Polymarket implied prob
        ctx.bookmaker_home_prob       float   bookmaker consensus (0–1)
        ctx.home_xg_avg               float   avg xG for in last 5
        ctx.away_xg_avg               float   avg xG for in last 5
        ctx.home_form                 list    ["W","D","L", ...]
        ctx.away_form                 list    ["W","D","L", ...]
        ctx.h2h_home_wins/away_wins   int
        ctx.data_quality              str     "full"|"partial"|"none"
    """
    market_prior = ctx.market.yes_price  # fallback: trust the market

    # --- Bookmaker signal ---
    if ctx.bookmaker_home_prob > 0:
        book_signal = ctx.bookmaker_home_prob
    else:
        book_signal = market_prior

    # --- xG signal ---
    # Expected goals ratio: home_xg / (home_xg + away_xg)
    total_xg = ctx.home_xg_avg + ctx.away_xg_avg
    if total_xg > 0:
        xg_signal = ctx.home_xg_avg / total_xg
    else:
        xg_signal = market_prior

    # --- Form signal ---
    def form_pts(form_list):
        return sum(3 if r == "W" else 1 if r == "D" else 0 for r in form_list)

    home_pts = form_pts(ctx.home_form)
    away_pts = form_pts(ctx.away_form)
    max_pts  = 15.0  # max in 5 games
    if (home_pts + away_pts) > 0:
        form_signal = home_pts / (home_pts + away_pts)
    else:
        form_signal = market_prior

    # --- Weighted blend ---
    estimated = (
        BOOKMAKER_WEIGHT * book_signal
        + XG_WEIGHT      * xg_signal
        + FORM_WEIGHT    * form_signal
    )

    # Clip to valid probability range
    return round(max(0.01, min(0.99, estimated)), 4)


# ── Bet Sizing (Kelly Criterion) ───────────────────────────────────────────
def kelly_size(prob: float, price: float, bankroll: float, max_bet: float) -> float:
    """
    Kelly criterion bet size, scaled by KELLY_FRACTION, capped at max_bet.
    f = (prob * (1/price - 1) - (1 - prob)) / (1/price - 1)
    """
    if price <= 0.01 or price >= 0.99:
        return 0.0
    b = (1.0 / price) - 1.0   # net odds
    f = (prob * b - (1 - prob)) / b
    f = max(0.0, f) * KELLY_FRACTION
    return round(min(f * bankroll, max_bet), 2)


# ── Main Strategy Entry Point ──────────────────────────────────────────────
def evaluate_markets(contexts: list, balance: float, max_bet: float) -> list[dict]:
    """
    Called by run.py each iteration.

    Args:
        contexts: list of MatchContext objects (from market_data.build_match_context)
        balance:  current USDC wallet balance
        max_bet:  per-bet maximum from .env

    Returns:
        list of bet instructions:
        [{token_id, side, amount_usd, reason, market_id, question}, ...]
    """
    import logging
    log = logging.getLogger(__name__)
    bets = []

    for ctx in contexts:
        if len(bets) >= MAX_CONCURRENT_BETS:
            break

        mkt = ctx.market

        # Skip low liquidity
        if mkt.liquidity_usd < MIN_LIQUIDITY_HARD:
            continue

        # Skip if data quality is insufficient
        quality_rank = {"full": 2, "partial": 1, "none": 0}
        if quality_rank.get(ctx.data_quality, 0) < quality_rank.get(MIN_DATA_QUALITY, 0):
            continue

        # YES side
        if MIN_MARKET_PRICE <= mkt.yes_price <= MAX_MARKET_PRICE:
            est_prob = estimate_probability(ctx)
            edge = est_prob - mkt.yes_price
            if edge >= EDGE_THRESHOLD:
                size = kelly_size(est_prob, mkt.yes_price, balance, max_bet)
                if size >= 1.0:
                    bets.append({
                        "token_id":   mkt.token_id_yes,
                        "side":       "BUY",
                        "amount_usd": size,
                        "reason":     f"YES edge={edge:.3f} est={est_prob:.3f} mkt={mkt.yes_price:.3f} quality={ctx.data_quality}",
                        "market_id":  mkt.market_id,
                        "question":   mkt.question,
                    })
                    log.info(f"BET YES: {mkt.question[:60]} | edge={edge:.3f} | ${size}")
                    continue  # don't also check NO for same market

        # NO side
        if MIN_MARKET_PRICE <= mkt.no_price <= MAX_MARKET_PRICE:
            est_prob_yes = estimate_probability(ctx)
            est_prob_no  = 1.0 - est_prob_yes
            no_edge = est_prob_no - mkt.no_price
            if no_edge >= EDGE_THRESHOLD:
                size = kelly_size(est_prob_no, mkt.no_price, balance, max_bet)
                if size >= 1.0:
                    bets.append({
                        "token_id":   mkt.token_id_no,
                        "side":       "BUY",
                        "amount_usd": size,
                        "reason":     f"NO edge={no_edge:.3f} est_no={est_prob_no:.3f} mkt={mkt.no_price:.3f} quality={ctx.data_quality}",
                        "market_id":  mkt.market_id,
                        "question":   mkt.question,
                    })
                    log.info(f"BET NO: {mkt.question[:60]} | edge={no_edge:.3f} | ${size}")

    return bets


# ── Live Event Handler (called by WebSocket thread) ────────────────────────
def handle_live_event(event: dict, market, balance: float) -> Optional[dict]:
    """
    Called when Polymarket Sports WebSocket delivers a live score update.
    event: raw WS message dict (type, scores, period, etc.)
    market: SoccerMarket object

    Returns a bet instruction dict or None.
    Agent can improve this logic to exploit in-play price drift.
    """
    # Baseline: no in-play betting (conservative default)
    # Agent may change this to react to specific in-play signals
    return None
