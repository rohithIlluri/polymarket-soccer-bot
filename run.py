"""
run.py — Autoresearch-style orchestrator.

Two threads:
  Thread 1 (main): Improvement loop — Claude edits trade.py, run strategy,
                   keep if metrics improve, revert if not.
  Thread 2 (async): Polymarket Sports WebSocket — live score listener.

Usage:
    uv run python run.py
"""
import os
import sys
import json
import time
import signal
import asyncio
import logging
import threading
import subprocess
import importlib.util
from datetime import datetime, timezone
from dotenv import load_dotenv
import anthropic

load_dotenv()
log = logging.getLogger(__name__)

RESULTS_TSV    = "results.tsv"
TRADE_FILE     = "trade.py"
PROGRAM_FILE   = "program.md"
INTERVAL       = int(os.getenv("BETTING_INTERVAL_MINUTES", 60)) * 60

# Shared market cache for WebSocket thread: game_id → SoccerMarket
_market_cache: dict = {}
_stop_event   = threading.Event()


# ── File helpers ───────────────────────────────────────────────────────────
def read_file(path: str) -> str:
    with open(path, "r") as f:
        return f.read()

def write_file(path: str, content: str):
    with open(path, "w") as f:
        f.write(content)

def read_recent_results(n: int = 15) -> str:
    try:
        with open(RESULTS_TSV) as f:
            lines = f.readlines()
        header = lines[0] if lines else ""
        data   = lines[1:] if len(lines) > 1 else []
        return header + "".join(data[-n:])
    except FileNotFoundError:
        return "(no results yet)"

def append_result(commit: str, metrics: dict, description: str, kept: bool):
    row = "\t".join([
        commit,
        str(metrics.get("pnl", 0)),
        str(metrics.get("win_rate", 0)),
        str(metrics.get("sharpe", 0)),
        str(metrics.get("n_bets", 0)),
        "KEPT" if kept else "REVERTED",
        description[:80],
        datetime.now(timezone.utc).isoformat(),
    ])
    with open(RESULTS_TSV, "a") as f:
        f.write(row + "\n")


# ── Git helpers ────────────────────────────────────────────────────────────
def git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, capture_output=True, text=True, check=check)

def current_commit() -> str:
    return git(["rev-parse", "--short", "HEAD"]).stdout.strip()

def git_commit_trade(message: str) -> str:
    git(["add", TRADE_FILE])
    result = git(["commit", "-m", message], check=False)
    if result.returncode != 0:
        # Nothing staged (no change)
        log.info("No changes in trade.py — skipping commit")
        return current_commit()
    return current_commit()

def git_revert_file_to(commit_hash: str):
    git(["checkout", commit_hash, "--", TRADE_FILE])
    git(["add", TRADE_FILE])
    git(["commit", "-m", f"revert: metrics worse, back to {commit_hash}"], check=False)

def metrics_score(m: dict) -> float:
    """Composite score used to decide keep vs revert."""
    return 0.6 * m.get("sharpe", 0) + 0.4 * (m.get("pnl", 0) / 10.0)


# ── Claude — strategy improvement ─────────────────────────────────────────
def ask_claude_for_new_strategy(current_trade: str, recent_results: str, program: str) -> str:
    """Returns full new content for trade.py."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    prompt = f"""You are an expert quantitative sports betting strategist specializing in prediction markets.

Here are your instructions and improvement objectives:
<program>
{program}
</program>

Here are the recent iteration results (tab-separated: commit | pnl | win_rate | sharpe | n_bets | status | description | timestamp):
<results>
{recent_results}
</results>

Here is the current trade.py:
<current_trade_py>
{current_trade}
</current_trade_py>

Based on the results, propose an improved version of trade.py that will increase the Sharpe ratio and P&L.
If there are no results yet, make a reasonable first improvement to the baseline.

Output ONLY the complete Python file contents. Start with:
# Change: <one sentence describing what you changed and why>

Do not include markdown fences. Do not add explanations outside the file.
All weights (BOOKMAKER_WEIGHT + XG_WEIGHT + FORM_WEIGHT) must sum to 1.0.
"""
    msg = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ── Strategy execution ─────────────────────────────────────────────────────
def run_strategy() -> dict:
    """
    Load trade.py, fetch markets, place bets.
    Returns current metrics dict.
    """
    from market_data import (
        fetch_soccer_markets, build_match_context,
        get_wallet_balance, place_bet,
        load_bet_history, append_bet, calculate_metrics,
        check_daily_loss_limit, BetRecord,
    )

    history = load_bet_history()
    if not check_daily_loss_limit(history):
        log.warning("Circuit breaker: daily loss limit hit. Skipping bets.")
        return calculate_metrics(history)

    balance = get_wallet_balance()
    max_bet = float(os.getenv("MAX_BET_USD", 10))
    log.info(f"Wallet balance: ${balance:.2f} USDC")

    # Hot-reload trade.py
    spec  = importlib.util.spec_from_file_location("trade", TRADE_FILE)
    trade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trade)

    markets = fetch_soccer_markets()
    if not markets:
        log.info("No active soccer markets found this cycle.")
        return calculate_metrics(history)

    # Update market cache for WebSocket thread
    global _market_cache
    _market_cache = {str(m.market_id): m for m in markets}

    # Build contexts
    log.info(f"Building context for {len(markets)} markets...")
    contexts = []
    for mkt in markets:
        try:
            ctx = build_match_context(mkt)
            contexts.append(ctx)
        except Exception as e:
            log.warning(f"Context build failed for {mkt.question[:40]}: {e}")

    # Get bet signals from trade.py
    bets = trade.evaluate_markets(contexts, balance, max_bet)
    log.info(f"Strategy signals: {len(bets)} bets")

    for bet in bets:
        if not check_daily_loss_limit():
            log.warning("Circuit breaker triggered mid-session. Stopping.")
            break
        try:
            place_bet(bet["token_id"], bet["amount_usd"], bet["side"])
            record = BetRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                market_id=bet["market_id"],
                question=bet["question"],
                side=bet["side"],
                price=0.0,
                size_usd=bet["amount_usd"],
                token_id=bet["token_id"],
            )
            append_bet(record)
            log.info(f"  Placed: {bet['question'][:50]} | {bet['reason'][:60]}")
        except Exception as e:
            log.error(f"  Order failed: {e}")

    return calculate_metrics(load_bet_history())


# ── WebSocket thread ───────────────────────────────────────────────────────
def _ws_thread_fn():
    """Runs the Polymarket Sports WebSocket listener in its own event loop."""
    from market_data import listen_sports_ws, place_bet, check_daily_loss_limit, BetRecord, append_bet

    async def _run():
        stop_async = asyncio.Event()

        def _on_event(event_data: dict, market):
            # Hot-reload trade.py for live event handling
            spec  = importlib.util.spec_from_file_location("trade", TRADE_FILE)
            trade = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(trade)
            if not check_daily_loss_limit():
                return
            balance = float(os.getenv("MAX_BET_USD", 10)) * 5  # rough estimate
            bet = trade.handle_live_event(event_data, market, balance)
            if bet:
                try:
                    place_bet(bet["token_id"], bet["amount_usd"], bet["side"])
                    record = BetRecord(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        market_id=bet["market_id"],
                        question=bet.get("question", ""),
                        side=bet["side"],
                        price=0.0,
                        size_usd=bet["amount_usd"],
                        token_id=bet["token_id"],
                    )
                    append_bet(record)
                    log.info(f"[WS] Live bet placed: {bet}")
                except Exception as e:
                    log.error(f"[WS] Order failed: {e}")

        # Monitor _stop_event and set stop_async accordingly
        async def _watch_stop():
            while not _stop_event.is_set():
                await asyncio.sleep(1)
            stop_async.set()

        await asyncio.gather(
            listen_sports_ws(_on_event, _market_cache, stop_async),
            _watch_stop(),
        )

    asyncio.run(_run())


# ── Main loop ──────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("Polymarket Soccer Bot — Karpathy autoresearch style")
    log.info("=" * 60)

    # Ensure results.tsv header
    if not os.path.exists(RESULTS_TSV):
        with open(RESULTS_TSV, "w") as f:
            f.write("commit\tpnl\twin_rate\tsharpe\tn_bets\tstatus\tdescription\ttimestamp\n")

    # Initial git commit if repo is empty
    try:
        git(["rev-parse", "HEAD"])
    except subprocess.CalledProcessError:
        git(["add", TRADE_FILE, PROGRAM_FILE, "market_data.py", "run.py",
             "pyproject.toml", ".gitignore"])
        git(["commit", "-m", "initial: baseline strategy"])

    # Start WebSocket listener thread
    ws_thread = threading.Thread(target=_ws_thread_fn, daemon=True, name="ws-listener")
    ws_thread.start()
    log.info("WebSocket listener thread started")

    # Graceful shutdown on SIGINT/SIGTERM
    def _shutdown(sig, frame):
        log.info("Shutting down...")
        _stop_event.set()
        sys.exit(0)
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    baseline_metrics = {"pnl": 0.0, "win_rate": 0.0, "sharpe": 0.0, "n_bets": 0}
    iteration = 0

    while not _stop_event.is_set():
        iteration += 1
        log.info(f"\n{'='*60}\nIteration {iteration}\n{'='*60}")

        prev_commit = current_commit()
        current_trade  = read_file(TRADE_FILE)
        recent_results = read_recent_results()
        program        = read_file(PROGRAM_FILE)

        # 1. Ask Claude for a better strategy
        log.info("Calling Claude for strategy improvement...")
        try:
            new_trade = ask_claude_for_new_strategy(current_trade, recent_results, program)
        except Exception as e:
            log.error(f"Claude API error: {e}. Skipping iteration.")
            time.sleep(INTERVAL)
            continue

        # 2. Write + commit
        write_file(TRADE_FILE, new_trade)
        description = new_trade.splitlines()[0].replace("# Change:", "").strip()
        new_commit  = git_commit_trade(f"iter-{iteration}: {description[:60]}")
        log.info(f"Committed as {new_commit}: {description[:60]}")

        # 3. Execute strategy
        log.info("Running strategy...")
        try:
            new_metrics = run_strategy()
        except Exception as e:
            log.error(f"Strategy execution error: {e}. Reverting.")
            git_revert_file_to(prev_commit)
            append_result(prev_commit, baseline_metrics, f"ERROR: {str(e)[:60]}", kept=False)
            time.sleep(INTERVAL)
            continue

        log.info(f"Metrics: pnl={new_metrics['pnl']} win_rate={new_metrics['win_rate']} sharpe={new_metrics['sharpe']} n_bets={new_metrics['n_bets']}")

        # 4. Keep or revert (like autoresearch keep-if-better)
        improved = metrics_score(new_metrics) > metrics_score(baseline_metrics)
        if improved:
            log.info(f"IMPROVED — keeping commit {new_commit}")
            baseline_metrics = new_metrics
            append_result(new_commit, new_metrics, description, kept=True)
        else:
            log.info(f"NO IMPROVEMENT — reverting to {prev_commit}")
            git_revert_file_to(prev_commit)
            append_result(new_commit, new_metrics, description, kept=False)

        log.info(f"Sleeping {INTERVAL // 60}m until next iteration...")
        for _ in range(INTERVAL):
            if _stop_event.is_set():
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
