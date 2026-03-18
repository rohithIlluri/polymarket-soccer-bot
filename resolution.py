"""
resolution.py — Bet outcome resolution against Polymarket Gamma API.

Queries settled markets, matches outcomes to bet history, calculates P&L,
and updates bet_history.jsonl atomically.
"""
import os
import json
import tempfile
import logging
import requests
from collections import defaultdict

log = logging.getLogger(__name__)

GAMMA_HOST = "https://gamma-api.polymarket.com"


def _fetch_market_resolution(market_id: str) -> dict | None:
    """
    Query Gamma API for a single market's resolution status.

    Returns dict with keys:
        resolved (bool), winning_token_id (str), outcome_prices (list[str])
    Or None if the market is not yet resolved or the request fails.
    """
    try:
        resp = requests.get(
            f"{GAMMA_HOST}/markets/{market_id}",
            timeout=10,
        )
        if not resp.ok:
            log.debug(f"Gamma API {resp.status_code} for market {market_id}")
            return None

        data = resp.json()

        # Check if market is resolved
        if not data.get("resolved", False):
            return None

        # Determine winning token
        outcomes = data.get("outcomes", [])
        outcome_prices = data.get("outcomePrices", [])

        winning_token_id = None
        if outcome_prices and outcomes:
            # The winning outcome has price ~1.0, losing has ~0.0
            for i, price_str in enumerate(outcome_prices):
                try:
                    if float(price_str) > 0.5:
                        token_ids = [
                            o.get("clobTokenId", "")
                            for o in (data.get("outcomes") if isinstance(data.get("outcomes"), list) and isinstance(data["outcomes"][0], dict) else [])
                        ]
                        if token_ids and i < len(token_ids):
                            winning_token_id = token_ids[i]
                            break
                except (ValueError, IndexError):
                    continue

        # Fallback: try clobTokenIds directly from market data
        if not winning_token_id:
            clob_token_ids = data.get("clobTokenIds", [])
            if clob_token_ids and outcome_prices:
                for i, price_str in enumerate(outcome_prices):
                    try:
                        if float(price_str) > 0.5 and i < len(clob_token_ids):
                            winning_token_id = clob_token_ids[i]
                            break
                    except (ValueError, IndexError):
                        continue

        if not winning_token_id:
            log.warning(f"Market {market_id} resolved but could not determine winner")
            return None

        return {
            "resolved": True,
            "winning_token_id": winning_token_id,
            "outcome_prices": outcome_prices,
        }

    except Exception as e:
        log.warning(f"Failed to fetch resolution for market {market_id}: {e}")
        return None


def resolve_bet_outcomes():
    """
    Resolve unresolved bets by checking Polymarket for settled markets.
    Updates bet_history.jsonl atomically with outcomes and P&L.
    """
    from market_data import (
        BET_HISTORY_PATH, BetRecord, load_bet_history,
        _history_lock,
    )

    with _history_lock:
        records = load_bet_history()

    if not records:
        return

    # Find unresolved bets
    unresolved_indices = [
        i for i, r in enumerate(records) if r.outcome is None
    ]
    if not unresolved_indices:
        return

    # Group by market_id to avoid duplicate API calls
    market_ids: dict[str, list[int]] = defaultdict(list)
    for i in unresolved_indices:
        market_ids[records[i].market_id].append(i)

    log.info(f"Resolving {len(unresolved_indices)} bets across {len(market_ids)} markets")

    updated = False
    for market_id, indices in market_ids.items():
        resolution = _fetch_market_resolution(market_id)
        if resolution is None:
            continue  # Market not yet resolved

        winning_token = resolution["winning_token_id"]
        for i in indices:
            rec = records[i]
            if rec.token_id == winning_token:
                rec.outcome = "WIN"
                # P&L = payout - cost. Payout = size / price (if price > 0).
                if rec.price > 0:
                    payout = rec.size_usd / rec.price
                    rec.pnl_usd = round(payout - rec.size_usd, 4)
                else:
                    # price=0 fallback: assume break-even to avoid bad data
                    rec.pnl_usd = 0.0
                    log.warning(f"Bet {rec.market_id} won but price=0, setting pnl=0")
            else:
                rec.outcome = "LOSS"
                rec.pnl_usd = round(-rec.size_usd, 4)

            log.info(
                f"Resolved: {rec.question[:50]} | {rec.outcome} | "
                f"P&L=${rec.pnl_usd}"
            )
            updated = True

    if not updated:
        return

    # Atomically rewrite bet_history.jsonl
    with _history_lock:
        try:
            dir_name = os.path.dirname(os.path.abspath(BET_HISTORY_PATH))
            fd, tmp_path = tempfile.mkstemp(
                dir=dir_name, prefix=".bet_history_", suffix=".tmp"
            )
            with os.fdopen(fd, "w") as f:
                for rec in records:
                    f.write(json.dumps(rec.__dict__) + "\n")
            os.replace(tmp_path, BET_HISTORY_PATH)
            log.info(f"Updated {sum(1 for r in records if r.outcome is not None)} resolved bets")
        except OSError as e:
            log.error(f"Failed to write resolved bet history: {e}")
            # Clean up temp file if it exists
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
