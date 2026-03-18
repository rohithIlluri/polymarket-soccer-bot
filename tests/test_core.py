"""
Tests for core bot logic — pure functions that don't require API access.
"""
import sys
import types
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

# Stub out heavy external dependencies that aren't needed for pure-function tests
_stubs = {
    "py_clob_client": MagicMock(),
    "py_clob_client.client": MagicMock(),
    "py_clob_client.clob_types": MagicMock(),
    "py_clob_client.order_builder": MagicMock(),
    "py_clob_client.order_builder.constants": MagicMock(),
}
for mod_name, mock in _stubs.items():
    if mod_name not in sys.modules:
        sys.modules[mod_name] = mock


# ── Sandbox Tests ─────────────────────────────────────────────────────────

class TestSandbox:
    def test_valid_trade_file(self):
        from sandbox import validate_trade_file
        source = '''
import logging
import math
from typing import Optional

log = logging.getLogger(__name__)

EDGE_THRESHOLD = 0.06

def estimate_probability(ctx) -> float:
    return max(0.01, min(0.99, ctx.market.yes_price))

def evaluate_markets(contexts, balance, max_bet):
    return []

def handle_live_event(event, market, balance):
    return None
'''
        ok, reason = validate_trade_file(source)
        assert ok, f"Valid trade file rejected: {reason}"

    def test_blocks_os_import(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("import os\nos.system('rm -rf /')")
        assert not ok
        assert "os" in reason

    def test_blocks_subprocess(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("import subprocess\nsubprocess.run(['ls'])")
        assert not ok
        assert "subprocess" in reason

    def test_blocks_from_import(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("from os import environ")
        assert not ok

    def test_blocks_eval(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("x = eval('1+1')")
        assert not ok
        assert "eval" in reason

    def test_blocks_exec(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("exec('import os')")
        assert not ok
        assert "exec" in reason

    def test_blocks_open(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("f = open('/etc/passwd')")
        assert not ok
        assert "open" in reason

    def test_blocks_dunder_globals(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("x = foo.__globals__")
        assert not ok
        assert "__globals__" in reason

    def test_blocks_dunder_import(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("m = __import__('os')")
        assert not ok

    def test_blocks_unknown_import(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("import requests")
        assert not ok
        assert "allowlist" in reason

    def test_allows_numpy(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("import numpy as np\nx = np.mean([1, 2])")
        assert ok, f"numpy should be allowed: {reason}"

    def test_allows_scipy(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("from scipy import stats")
        assert ok, f"scipy should be allowed: {reason}"

    def test_syntax_error(self):
        from sandbox import validate_trade_file
        ok, reason = validate_trade_file("def foo(:\n  pass")
        assert not ok
        assert "Syntax error" in reason


# ── Kelly Sizing Tests ────────────────────────────────────────────────────

class TestKellySizing:
    def _get_kelly(self):
        import sys
        sys.path.insert(0, "/tmp/polymarket-soccer-bot")
        from trade import kelly_size
        return kelly_size

    def test_positive_edge(self):
        kelly = self._get_kelly()
        # prob=0.6, price=0.4 → positive edge
        size = kelly(0.6, 0.4, 1000.0, 50.0)
        assert size > 0
        assert size <= 50.0

    def test_no_edge(self):
        kelly = self._get_kelly()
        # prob equals price → no edge
        size = kelly(0.5, 0.5, 1000.0, 50.0)
        assert size == 0.0

    def test_negative_edge(self):
        kelly = self._get_kelly()
        # prob < price → negative edge
        size = kelly(0.3, 0.5, 1000.0, 50.0)
        assert size == 0.0

    def test_extreme_price_low(self):
        kelly = self._get_kelly()
        size = kelly(0.5, 0.01, 1000.0, 50.0)
        assert size == 0.0

    def test_extreme_price_high(self):
        kelly = self._get_kelly()
        size = kelly(0.5, 0.99, 1000.0, 50.0)
        assert size == 0.0

    def test_capped_at_max_bet(self):
        kelly = self._get_kelly()
        # Very strong edge with large bankroll
        size = kelly(0.9, 0.2, 100000.0, 10.0)
        assert size <= 10.0


# ── Metrics Tests ─────────────────────────────────────────────────────────

class TestMetrics:
    def _make_record(self, pnl, ts="2025-01-01T12:00:00"):
        from market_data import BetRecord
        return BetRecord(
            timestamp=ts,
            market_id="test",
            question="test",
            side="BUY",
            price=0.5,
            size_usd=10.0,
            token_id="tok",
            outcome="WIN" if pnl > 0 else "LOSS",
            pnl_usd=pnl,
        )

    def test_empty_records(self):
        from market_data import calculate_metrics
        m = calculate_metrics([])
        assert m == {"pnl": 0.0, "win_rate": 0.0, "sharpe": 0.0, "n_bets": 0}

    def test_unresolved_records(self):
        from market_data import BetRecord, calculate_metrics
        rec = BetRecord(
            timestamp="2025-01-01T12:00:00",
            market_id="test", question="test", side="BUY",
            price=0.5, size_usd=10.0, token_id="tok",
        )
        m = calculate_metrics([rec])
        assert m["n_bets"] == 0

    def test_all_wins(self):
        from market_data import calculate_metrics
        records = [self._make_record(5.0) for _ in range(3)]
        m = calculate_metrics(records)
        assert m["pnl"] == 15.0
        assert m["win_rate"] == 1.0
        assert m["n_bets"] == 3

    def test_mixed_results(self):
        from market_data import calculate_metrics
        records = [
            self._make_record(10.0, "2025-01-01T12:00:00"),
            self._make_record(-5.0, "2025-01-02T12:00:00"),
            self._make_record(3.0, "2025-01-03T12:00:00"),
        ]
        m = calculate_metrics(records)
        assert m["pnl"] == 8.0
        assert abs(m["win_rate"] - 0.6667) < 0.01
        assert m["n_bets"] == 3


# ── Daily Loss Limit Tests ────────────────────────────────────────────────

class TestDailyLossLimit:
    def _make_record(self, pnl, ts=None):
        from market_data import BetRecord
        if ts is None:
            ts = datetime.now(timezone.utc).isoformat()
        return BetRecord(
            timestamp=ts,
            market_id="test", question="test", side="BUY",
            price=0.5, size_usd=10.0, token_id="tok",
            outcome="LOSS", pnl_usd=pnl,
        )

    def test_safe_when_no_losses(self):
        from market_data import check_daily_loss_limit
        assert check_daily_loss_limit([]) is True

    def test_safe_within_limit(self):
        from market_data import check_daily_loss_limit
        records = [self._make_record(-10.0)]
        assert check_daily_loss_limit(records) is True

    def test_breached(self):
        from market_data import check_daily_loss_limit
        # Default limit is $50
        records = [self._make_record(-30.0), self._make_record(-25.0)]
        assert check_daily_loss_limit(records) is False

    def test_old_losses_ignored(self):
        from market_data import check_daily_loss_limit
        records = [self._make_record(-100.0, "2020-01-01T12:00:00")]
        assert check_daily_loss_limit(records) is True


# ── Metrics Score Tests ───────────────────────────────────────────────────

class TestMetricsScore:
    def test_zero_metrics(self):
        import sys
        sys.path.insert(0, "/tmp/polymarket-soccer-bot")
        from run import metrics_score
        assert metrics_score({"sharpe": 0, "pnl": 0}) == 0.0

    def test_positive_metrics(self):
        from run import metrics_score
        score = metrics_score({"sharpe": 1.0, "pnl": 10.0})
        assert score > 0

    def test_sharpe_weighted_more(self):
        from run import metrics_score
        # Same total contribution but sharpe should dominate
        high_sharpe = metrics_score({"sharpe": 2.0, "pnl": 0.0})
        high_pnl = metrics_score({"sharpe": 0.0, "pnl": 30.0})
        assert high_sharpe == pytest.approx(1.2)
        assert high_pnl == pytest.approx(1.2)
