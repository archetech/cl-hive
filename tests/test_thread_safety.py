"""
Tests for thread safety fixes from audit 2026-02-10.

Tests cover:
- M-3: LiquidityCoordinator rate dict lock under concurrent access
"""

import threading
import time
import pytest
from unittest.mock import MagicMock


class TestLiquidityCoordinatorRateLock:
    """Test that LiquidityCoordinator rate limiting is thread-safe."""

    def test_has_rate_lock(self):
        """Verify the rate lock was added."""
        from modules.liquidity_coordinator import LiquidityCoordinator

        db = MagicMock()
        plugin = MagicMock()
        lc = LiquidityCoordinator(database=db, plugin=plugin, our_pubkey="02" + "aa" * 32)
        assert hasattr(lc, '_rate_lock')
        assert isinstance(lc._rate_lock, type(threading.Lock()))

    def test_concurrent_rate_limiting(self):
        """Test rate limiting under concurrent access."""
        from modules.liquidity_coordinator import LiquidityCoordinator
        from modules.protocol import LIQUIDITY_NEED_RATE_LIMIT

        db = MagicMock()
        plugin = MagicMock()
        lc = LiquidityCoordinator(database=db, plugin=plugin, our_pubkey="02" + "aa" * 32)
        errors = []
        stop = threading.Event()

        def check_rates():
            while not stop.is_set():
                try:
                    sender = f"02{'bb' * 32}"
                    lc._check_rate_limit(sender, lc._need_rate, LIQUIDITY_NEED_RATE_LIMIT)
                    lc._record_message(sender, lc._need_rate)
                except Exception as e:
                    errors.append(str(e))

        threads = [threading.Thread(target=check_rates, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()

        time.sleep(0.3)
        stop.set()
        for t in threads:
            t.join(timeout=2)

        assert errors == [], f"Rate limit thread safety errors: {errors}"
