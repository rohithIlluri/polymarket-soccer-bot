"""
market_data.py — IMMUTABLE infrastructure layer.
DO NOT MODIFY — this file is never edited by the agent.

Provides:
- Polymarket market discovery (Gamma API)
- Live score feed (Polymarket Sports WebSocket)
- Pre-match stats (API-Football + Understat + soccerdata/FBref)
- Bookmaker edge detection (The Odds API)
- Order execution (py-clob-client → Polymarket CLOB)
- P&L tracking and risk controls
"""
import os
import json
import time
import logging
import asyncio
import requests
import numpy as np
import websockets
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")],
)
log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
POLYMARKET_HOST      = "https://clob.polymarket.com"
GAMMA_HOST           = "https://gamma-api.polymarket.com"
SPORTS_WS_URL        = "wss://sports-api.polymarket.com/ws"
CHAIN_ID             = 137   # Polygon mainnet
USDC_DECIMALS        = 6
API_FOOTBALL_BASE    = "https://v3.football.api-sports.io"
ODDS_API_BASE        = "https://api.the-odds-api.com/v4"

# Team name aliases: Polymarket names → football API names
TEAM_NAME_ALIASES: dict[str, list[str]] = {
    "Manchester City": ["Man City", "Man. City"],
    "Manchester United": ["Man Utd", "Man United"],
    "Atletico Madrid": ["Atlético Madrid", "Atletico de Madrid"],
    "Paris Saint-Germain": ["PSG", "Paris SG"],
    "Inter Milan": ["Inter", "FC Internazionale"],
    "AC Milan": ["Milan", "AC Milan"],
}


# ── Dataclasses ────────────────────────────────────────────────────────────
@dataclass
class SoccerMarket:
    market_id: str
    token_id_yes: str
    token_id_no: str
    question: str
    yes_price: float
    no_price: float
    liquidity_usd: float
    end_date_iso: str
    home_team: str
    away_team: str
    competition: str


@dataclass
class MatchContext:
    market: SoccerMarket
    home_form: list[str] = field(default_factory=list)   # ["W","D","L","W","W"]
    away_form: list[str] = field(default_factory=list)
    home_xg_avg: float = 0.0      # avg xG last 5 home games
    away_xg_avg: float = 0.0
    bookmaker_home_prob: float = 0.0   # consensus bookmaker implied prob
    bookmaker_away_prob: float = 0.0
    h2h_home_wins: int = 0
    h2h_away_wins: int = 0
    h2h_draws: int = 0
    data_quality: str = "none"    # "full" | "partial" | "none"


@dataclass
class BetRecord:
    timestamp: str
    market_id: str
    question: str
    side: str           # "YES" or "NO"
    price: float
    size_usd: float
    token_id: str
    outcome: Optional[str] = None    # "WIN" | "LOSS" | None (unresolved)
    pnl_usd: Optional[float] = None


# ── Polymarket Client ──────────────────────────────────────────────────────
def get_clob_client() -> ClobClient:
    creds = ApiCreds(
        api_key=os.getenv("POLY_API_KEY", ""),
        api_secret=os.getenv("POLY_SECRET", ""),
        api_passphrase=os.getenv("POLY_PASSPHRASE", ""),
    )
    return ClobClient(
        POLYMARKET_HOST,
        key=os.getenv("POLYGON_PRIVATE_KEY", ""),
        chain_id=CHAIN_ID,
        creds=creds,
    )


# ── Market Discovery (Gamma API) ───────────────────────────────────────────
def fetch_soccer_markets() -> list[SoccerMarket]:
    """
    Fetch active pre-match soccer markets from Polymarket Gamma API.
    Filters: active, not resolved, sufficient liquidity, soccer tag.
    """
    min_liq = float(os.getenv("MIN_LIQUIDITY_USD", 500))
    markets: list[SoccerMarket] = []

    try:
        # Discover soccer tag IDs
        tags_resp = requests.get(f"{GAMMA_HOST}/tags", timeout=10)
        tags_resp.raise_for_status()
        soccer_tag_ids = [
            t["id"] for t in tags_resp.json()
            if any(kw in t.get("label", "").lower() for kw in ["soccer", "football"])
        ]

        for tag_id in soccer_tag_ids:
            resp = requests.get(
                f"{GAMMA_HOST}/events",
                params={"tag_id": tag_id, "active": "true", "closed": "false", "limit": 100},
                timeout=10,
            )
            if not resp.ok:
                continue
            for event in resp.json().get("data", []):
                for mkt in event.get("markets", []):
                    outcomes = mkt.get("outcomes", [])
                    if len(outcomes) < 2:
                        continue
                    liquidity = float(mkt.get("liquidity", 0))
                    if liquidity < min_liq:
                        continue
                    end_iso = mkt.get("endDate", "")
                    # Only pre-match: end_date must be > 10 min from now
                    try:
                        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                        if (end_dt - datetime.now(timezone.utc)).total_seconds() < 600:
                            continue
                    except Exception:
                        continue
                    prices = mkt.get("outcomePrices", ["0.5", "0.5"])
                    markets.append(SoccerMarket(
                        market_id=mkt["id"],
                        token_id_yes=outcomes[0].get("clobTokenId", ""),
                        token_id_no=outcomes[1].get("clobTokenId", ""),
                        question=mkt.get("question", ""),
                        yes_price=float(prices[0]),
                        no_price=float(prices[1]),
                        liquidity_usd=liquidity,
                        end_date_iso=end_iso,
                        home_team=event.get("homeTeam", ""),
                        away_team=event.get("awayTeam", ""),
                        competition=event.get("league", ""),
                    ))
    except Exception as e:
        log.error(f"fetch_soccer_markets error: {e}")

    log.info(f"Fetched {len(markets)} live pre-match soccer markets")
    return markets


# ── Pre-Match Stats: API-Football ──────────────────────────────────────────
def _api_football_get(endpoint: str, params: dict) -> dict:
    headers = {"x-rapidapi-key": os.getenv("API_FOOTBALL_KEY", ""), "x-rapidapi-host": "v3.football.api-sports.io"}
    resp = requests.get(f"{API_FOOTBALL_BASE}/{endpoint}", headers=headers, params=params, timeout=10)
    if resp.ok:
        return resp.json()
    log.warning(f"API-Football {endpoint} failed: {resp.status_code}")
    return {}


def _normalize_team_name(name: str) -> str:
    for canonical, aliases in TEAM_NAME_ALIASES.items():
        if name in aliases:
            return canonical
    return name


def fetch_team_id(team_name: str) -> Optional[int]:
    name = _normalize_team_name(team_name)
    data = _api_football_get("teams", {"search": name})
    teams = data.get("response", [])
    return teams[0]["team"]["id"] if teams else None


def fetch_recent_form(team_id: int, last_n: int = 5) -> list[str]:
    """Returns list of results e.g. ['W','D','L','W','W'] most recent first."""
    data = _api_football_get("fixtures", {"team": team_id, "last": last_n, "status": "FT"})
    form = []
    for fixture in data.get("response", []):
        teams = fixture.get("teams", {})
        goals = fixture.get("goals", {})
        home_id = teams.get("home", {}).get("id")
        home_goals = goals.get("home", 0) or 0
        away_goals = goals.get("away", 0) or 0
        if home_id == team_id:
            if home_goals > away_goals:
                form.append("W")
            elif home_goals == away_goals:
                form.append("D")
            else:
                form.append("L")
        else:
            if away_goals > home_goals:
                form.append("W")
            elif away_goals == home_goals:
                form.append("D")
            else:
                form.append("L")
    return form


def fetch_h2h(home_id: int, away_id: int, last_n: int = 10) -> dict:
    """Returns h2h record between two teams."""
    data = _api_football_get("fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": last_n})
    home_wins = away_wins = draws = 0
    for fixture in data.get("response", []):
        teams = fixture.get("teams", {})
        goals = fixture.get("goals", {})
        h_id = teams.get("home", {}).get("id")
        h_goals = goals.get("home", 0) or 0
        a_goals = goals.get("away", 0) or 0
        if h_goals > a_goals:
            if h_id == home_id:
                home_wins += 1
            else:
                away_wins += 1
        elif h_goals == a_goals:
            draws += 1
        else:
            if h_id == home_id:
                away_wins += 1
            else:
                home_wins += 1
    return {"home_wins": home_wins, "away_wins": away_wins, "draws": draws}


# ── Pre-Match Stats: Understat xG ─────────────────────────────────────────
def fetch_xg_stats(team_name: str) -> dict:
    """Returns avg xG for and against from last 5 matches via understatapi."""
    try:
        from understatapi import UnderstatClient
        client = UnderstatClient()
        # understatapi returns league/team search
        # This is best-effort: name matching may fail for some teams
        results = client.team(team=team_name).get_match_data()
        if not results:
            return {"xg_for": 0.0, "xg_against": 0.0}
        recent = results[-5:]
        xg_for     = float(np.mean([float(m.get("xG", 0)) for m in recent]))
        xg_against = float(np.mean([float(m.get("xGA", 0)) for m in recent]))
        return {"xg_for": round(xg_for, 3), "xg_against": round(xg_against, 3)}
    except Exception as e:
        log.debug(f"xG fetch failed for {team_name}: {e}")
        return {"xg_for": 0.0, "xg_against": 0.0}


# ── Bookmaker Odds (The Odds API) ─────────────────────────────────────────
def fetch_bookmaker_odds(home_team: str, away_team: str) -> dict:
    """
    Returns implied probabilities from bookmaker consensus.
    Uses The Odds API free tier (500 req/month).
    """
    default = {"home_prob": 0.0, "away_prob": 0.0, "draw_prob": 0.0}
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return default
    try:
        resp = requests.get(
            f"{ODDS_API_BASE}/sports/soccer/odds",
            params={
                "apiKey": api_key,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal",
                "bookmakers": "betfair,pinnacle,bet365",
            },
            timeout=10,
        )
        if not resp.ok:
            return default
        events = resp.json()
        home_n = _normalize_team_name(home_team).lower()
        away_n = _normalize_team_name(away_team).lower()
        for event in events:
            if (home_n in event.get("home_team", "").lower() or
                    away_n in event.get("away_team", "").lower()):
                all_home = []
                all_away = []
                all_draw = []
                for bookmaker in event.get("bookmakers", []):
                    for market in bookmaker.get("markets", []):
                        if market["key"] == "h2h":
                            for outcome in market.get("outcomes", []):
                                name_l = outcome["name"].lower()
                                odd = float(outcome["price"])
                                if odd <= 1.0:
                                    continue
                                imp = 1.0 / odd
                                if "draw" in name_l:
                                    all_draw.append(imp)
                                elif home_n in name_l:
                                    all_home.append(imp)
                                elif away_n in name_l:
                                    all_away.append(imp)
                if all_home and all_away:
                    raw_home = float(np.mean(all_home))
                    raw_away = float(np.mean(all_away))
                    raw_draw = float(np.mean(all_draw)) if all_draw else 0.0
                    total = raw_home + raw_away + raw_draw or 1.0
                    return {
                        "home_prob": round(raw_home / total, 4),
                        "away_prob": round(raw_away / total, 4),
                        "draw_prob": round(raw_draw / total, 4),
                    }
    except Exception as e:
        log.debug(f"Odds API error: {e}")
    return default


# ── Aggregated Match Context ───────────────────────────────────────────────
def build_match_context(market: SoccerMarket) -> MatchContext:
    """Combines all pre-match data sources into a MatchContext for trade.py."""
    ctx = MatchContext(market=market)

    # API-Football form + H2H
    home_id = fetch_team_id(market.home_team)
    away_id = fetch_team_id(market.away_team)
    if home_id:
        ctx.home_form = fetch_recent_form(home_id)
    if away_id:
        ctx.away_form = fetch_recent_form(away_id)
    if home_id and away_id:
        h2h = fetch_h2h(home_id, away_id)
        ctx.h2h_home_wins = h2h["home_wins"]
        ctx.h2h_away_wins = h2h["away_wins"]
        ctx.h2h_draws     = h2h["draws"]

    # Understat xG
    home_xg = fetch_xg_stats(market.home_team)
    away_xg = fetch_xg_stats(market.away_team)
    ctx.home_xg_avg = home_xg["xg_for"]
    ctx.away_xg_avg = away_xg["xg_for"]

    # Bookmaker odds
    odds = fetch_bookmaker_odds(market.home_team, market.away_team)
    ctx.bookmaker_home_prob = odds["home_prob"]
    ctx.bookmaker_away_prob = odds["away_prob"]

    # Data quality assessment
    has_form    = bool(ctx.home_form or ctx.away_form)
    has_odds    = ctx.bookmaker_home_prob > 0
    has_xg      = ctx.home_xg_avg > 0 or ctx.away_xg_avg > 0
    if has_form and has_odds and has_xg:
        ctx.data_quality = "full"
    elif has_form or has_odds:
        ctx.data_quality = "partial"
    else:
        ctx.data_quality = "none"

    return ctx


# ── Polymarket Sports WebSocket (live scores) ──────────────────────────────
async def listen_sports_ws(on_event_fn, market_cache: dict, stop_event: asyncio.Event):
    """
    Connects to wss://sports-api.polymarket.com/ws and calls on_event_fn
    with (event_data, matching_market) whenever a soccer score changes.

    market_cache: dict mapping game_id → SoccerMarket (populated by main loop)
    stop_event: asyncio.Event — set to stop this coroutine
    """
    while not stop_event.is_set():
        try:
            async with websockets.connect(SPORTS_WS_URL, ping_interval=None) as ws:
                log.info("Sports WebSocket connected")
                while not stop_event.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=15)
                        data = json.loads(msg)
                        # Server heartbeat
                        if data.get("type") == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                            continue
                        # Score update events
                        if data.get("type") in ("score_update", "match_update", "goal"):
                            game_id = str(data.get("game_id", data.get("id", "")))
                            matching = market_cache.get(game_id)
                            if matching:
                                try:
                                    on_event_fn(data, matching)
                                except Exception as e:
                                    log.error(f"on_event_fn error: {e}")
                    except asyncio.TimeoutError:
                        # Send keepalive
                        await ws.send(json.dumps({"type": "pong"}))
        except Exception as e:
            log.warning(f"Sports WS disconnected: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


# ── Order Execution ────────────────────────────────────────────────────────
def place_bet(token_id: str, amount_usd: float, side: str) -> dict:
    """
    Place a Fill-or-Kill market order on Polymarket.
    side: "BUY" or "SELL"
    Returns the API response dict.
    """
    client = get_clob_client()
    order_side = BUY if side == "BUY" else SELL
    mo = MarketOrderArgs(
        token_id=token_id,
        amount=amount_usd,
        side=order_side,
    )
    signed = client.create_market_order(mo)
    resp = client.post_order(signed, OrderType.FOK)
    log.info(f"Order: token={token_id[:8]}.. amount=${amount_usd} side={side} resp={resp}")
    return resp


def get_wallet_balance() -> float:
    """Returns USDC balance on Polymarket in dollars."""
    client = get_clob_client()
    info = client.get_balance_allowance()
    raw = int(info.get("balance", 0))
    return raw / (10 ** USDC_DECIMALS)


# ── Bet History ────────────────────────────────────────────────────────────
BET_HISTORY_PATH = "bet_history.jsonl"


def load_bet_history() -> list[BetRecord]:
    records = []
    try:
        with open(BET_HISTORY_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(BetRecord(**json.loads(line)))
    except FileNotFoundError:
        pass
    return records


def append_bet(record: BetRecord):
    with open(BET_HISTORY_PATH, "a") as f:
        f.write(json.dumps(record.__dict__) + "\n")


# ── Risk Controls ──────────────────────────────────────────────────────────
def check_daily_loss_limit(records: Optional[list[BetRecord]] = None) -> bool:
    """Returns True if SAFE to bet (daily loss limit not breached)."""
    if records is None:
        records = load_bet_history()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = sum(
        r.pnl_usd for r in records
        if r.timestamp.startswith(today) and r.pnl_usd is not None
    )
    max_loss = float(os.getenv("MAX_DAILY_LOSS_USD", 50))
    if today_pnl < -max_loss:
        log.warning(f"Daily loss limit breached: {today_pnl:.2f} USD. Halting.")
        return False
    return True


# ── Metrics ────────────────────────────────────────────────────────────────
def calculate_metrics(records: Optional[list[BetRecord]] = None) -> dict:
    """
    Compute P&L, win_rate, Sharpe from resolved bets.
    Sharpe = mean(daily_pnl) / std(daily_pnl) * sqrt(365)
    """
    if records is None:
        records = load_bet_history()
    resolved = [r for r in records if r.pnl_usd is not None]
    if not resolved:
        return {"pnl": 0.0, "win_rate": 0.0, "sharpe": 0.0, "n_bets": 0}

    pnls = [r.pnl_usd for r in resolved]
    total_pnl = sum(pnls)
    win_rate  = sum(1 for p in pnls if p > 0) / len(pnls)

    from collections import defaultdict
    daily: dict[str, float] = defaultdict(float)
    for r in resolved:
        daily[r.timestamp[:10]] += r.pnl_usd
    daily_vals = list(daily.values())
    if len(daily_vals) > 1:
        sharpe = float(np.mean(daily_vals) / (np.std(daily_vals) + 1e-9) * np.sqrt(365))
    else:
        sharpe = 0.0

    return {
        "pnl":      round(total_pnl, 4),
        "win_rate": round(win_rate, 4),
        "sharpe":   round(sharpe, 4),
        "n_bets":   len(resolved),
    }
