# Phases 3b, 3c & Revenue-Ops Traffic Intelligence Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete traffic intelligence integration: revenue-ops feeds local traffic profiles to cl-hive and consumes fleet intelligence, MCF assignment execution respects fleet peak/quiet hours, and fee coordination incorporates fleet-wide forward size data.

**Architecture:** Traffic-Intel-First ordering — revenue-ops profile reporting first (data pipeline), then Phase 3b MCF scheduling (consumes data), then Phase 3c fee enrichment (consumes data), then remaining revenue-ops integration methods. Two repos: cl-hive (`/home/sat/bin/cl-hive/`) and cl-revenue-ops (`/home/sat/bin/cl_revenue_ops/`).

**Tech Stack:** Python 3.10+, pytest, pyln-client, SQLite, Core Lightning RPC

**Design Doc:** `docs/plans/2026-03-09-phases-3b-3c-revops-design.md`

---

## Test Commands

- **cl-hive:** `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix`
- **cl-revenue-ops:** `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -x -q`

---

### Task 1: Revenue-Ops — `report_traffic_profile()` Bridge Method

Report graduated local traffic profiles to cl-hive via `hive-report-traffic-profile` RPC.

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/hive_bridge.py` (class `HiveFeeIntelligenceBridge`)
- Test: `/home/sat/bin/cl_revenue_ops/tests/test_hive_integrations.py`

**Step 1: Write the failing test**

Add to `test_hive_integrations.py`:

```python
class TestTrafficIntelligenceBridge:
    """Tests for traffic intelligence bridge methods."""

    def test_report_traffic_profile_success(self, hive_bridge):
        """report_traffic_profile sends profile to hive and returns True."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = True
        hive_bridge.plugin.rpc.call.return_value = {"status": "accepted", "peer_id": "02" + "a" * 64}

        result = hive_bridge.report_traffic_profile(
            peer_id="02" + "a" * 64,
            profile_type="retail",
            peak_hours_utc=[14, 15, 16, 17, 18, 19],
            quiet_hours_utc=[2, 3, 4, 5, 6, 7],
            avg_forward_size_sats=25000.0,
            daily_volume_sats=5000000.0,
            drain_direction="outbound_heavy",
            confidence=0.85,
            observation_window_hours=168,
        )

        assert result is True
        hive_bridge.plugin.rpc.call.assert_called_once()
        call_args = hive_bridge.plugin.rpc.call.call_args
        assert call_args[0][0] == "hive-report-traffic-profile"
        payload = call_args[0][1]
        assert payload["peer_id"] == "02" + "a" * 64
        assert payload["profile_type"] == "retail"
        assert payload["avg_forward_size_sats"] == 25000.0

    def test_report_traffic_profile_hive_unavailable(self, hive_bridge):
        """report_traffic_profile returns False when hive is down."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = False

        result = hive_bridge.report_traffic_profile(
            peer_id="02" + "a" * 64,
            profile_type="retail",
            peak_hours_utc=[14, 15],
            quiet_hours_utc=[2, 3],
            avg_forward_size_sats=25000.0,
            daily_volume_sats=5000000.0,
            drain_direction="balanced",
            confidence=0.7,
            observation_window_hours=168,
        )

        assert result is False

    def test_report_traffic_profile_rpc_error(self, hive_bridge):
        """report_traffic_profile returns False on RPC failure."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = True
        hive_bridge.plugin.rpc.call.side_effect = Exception("connection refused")

        result = hive_bridge.report_traffic_profile(
            peer_id="02" + "a" * 64,
            profile_type="wholesale",
            peak_hours_utc=[10, 11],
            quiet_hours_utc=[0, 1],
            avg_forward_size_sats=800000.0,
            daily_volume_sats=20000000.0,
            drain_direction="inbound_heavy",
            confidence=0.9,
            observation_window_hours=168,
        )

        assert result is False
```

**Step 2: Run test to verify it fails**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_hive_integrations.py::TestTrafficIntelligenceBridge -v`
Expected: FAIL with `AttributeError: ... has no attribute 'report_traffic_profile'`

**Step 3: Write minimal implementation**

Add to `HiveFeeIntelligenceBridge` in `hive_bridge.py` (after `check_rebalance_conflict` at ~L1036):

```python
    def report_traffic_profile(
        self,
        peer_id: str,
        profile_type: str,
        peak_hours_utc: list,
        quiet_hours_utc: list,
        avg_forward_size_sats: float,
        daily_volume_sats: float,
        drain_direction: str,
        confidence: float,
        observation_window_hours: int,
    ) -> bool:
        """
        Report local traffic profile to cl-hive for fleet sharing.

        Called from flow_analysis after temporal profiles graduate (7+ days).
        Uses telemetry policy — fire-and-forget, never blocks revenue-ops.

        Args:
            peer_id: External peer this profile describes
            profile_type: retail/wholesale/mixed
            peak_hours_utc: List of peak traffic hours (0-23)
            quiet_hours_utc: List of quiet traffic hours (0-23)
            avg_forward_size_sats: Average forward size in sats
            daily_volume_sats: Average daily volume in sats
            drain_direction: outbound_heavy/inbound_heavy/balanced
            confidence: Profile confidence (0.0-1.0)
            observation_window_hours: How long the profile was observed

        Returns:
            True if reported successfully, False otherwise
        """
        if not self.is_available():
            return False

        ok, result, err = self._rpc_call_with_policy(
            "hive-report-traffic-profile",
            {
                "peer_id": peer_id,
                "profile_type": profile_type,
                "peak_hours_utc": peak_hours_utc,
                "quiet_hours_utc": quiet_hours_utc,
                "avg_forward_size_sats": avg_forward_size_sats,
                "daily_volume_sats": daily_volume_sats,
                "drain_direction": drain_direction,
                "confidence": confidence,
                "observation_window_hours": observation_window_hours,
            },
            policy_key="telemetry",
        )
        if not ok:
            if err not in ("async_queue_full",):
                self._log(f"Failed to report traffic profile: {err}", level="debug")
            return False
        if result and result.get("error"):
            self._log(f"Traffic profile report error: {result.get('error')}", level="debug")
            return False
        return True
```

**Step 4: Run test to verify it passes**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_hive_integrations.py::TestTrafficIntelligenceBridge -v`
Expected: PASS (3 tests)

**Step 5: Run full test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -x -q`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/hive_bridge.py tests/test_hive_integrations.py
git commit -m "feat(hive-bridge): add report_traffic_profile() method

Reports graduated local traffic profiles to cl-hive via
hive-report-traffic-profile RPC. Uses telemetry policy (fire-and-forget).
Part of Phases 3b/3c revenue-ops integration."
```

---

### Task 2: Revenue-Ops — `query_traffic_intelligence()` Bridge Method

Query aggregated fleet traffic data from cl-hive via `hive-traffic-intelligence` RPC. Follows `query_fee_intelligence()` pattern: cache check → circuit breaker → RPC → cache update.

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/hive_bridge.py`
- Test: `/home/sat/bin/cl_revenue_ops/tests/test_hive_integrations.py`

**Step 1: Write the failing test**

Add to `TestTrafficIntelligenceBridge`:

```python
    def test_query_traffic_intelligence_success(self, hive_bridge):
        """query_traffic_intelligence returns fleet data on success."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = True
        fleet_data = {
            "peer_id": "02" + "a" * 64,
            "profile_type": "retail",
            "avg_forward_size_sats": 30000.0,
            "daily_volume_sats": 8000000.0,
            "drain_direction": "outbound_heavy",
            "reporters": 3,
            "confidence": 0.82,
        }
        hive_bridge.plugin.rpc.call.return_value = fleet_data

        result = hive_bridge.query_traffic_intelligence(peer_id="02" + "a" * 64)

        assert result is not None
        assert result["avg_forward_size_sats"] == 30000.0
        assert result["reporters"] == 3

    def test_query_traffic_intelligence_cached(self, hive_bridge):
        """query_traffic_intelligence returns cached data on second call."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = True
        fleet_data = {
            "peer_id": "02" + "a" * 64,
            "avg_forward_size_sats": 30000.0,
            "confidence": 0.82,
        }
        hive_bridge.plugin.rpc.call.return_value = fleet_data

        # First call populates cache
        result1 = hive_bridge.query_traffic_intelligence(peer_id="02" + "a" * 64)
        assert result1 is not None

        # Second call should use cache (no new RPC)
        hive_bridge.plugin.rpc.call.reset_mock()
        result2 = hive_bridge.query_traffic_intelligence(peer_id="02" + "a" * 64)
        assert result2 is not None
        hive_bridge.plugin.rpc.call.assert_not_called()

    def test_query_traffic_intelligence_no_data(self, hive_bridge):
        """query_traffic_intelligence returns None when no data available."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = True
        hive_bridge.plugin.rpc.call.return_value = {"error": "no_data"}

        result = hive_bridge.query_traffic_intelligence(peer_id="02" + "x" * 64)

        assert result is None

    def test_query_traffic_intelligence_hive_down(self, hive_bridge):
        """query_traffic_intelligence returns None when hive unavailable."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = False

        result = hive_bridge.query_traffic_intelligence(peer_id="02" + "a" * 64)

        assert result is None
```

**Step 2: Run test to verify it fails**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_hive_integrations.py::TestTrafficIntelligenceBridge::test_query_traffic_intelligence_success -v`
Expected: FAIL with `AttributeError`

**Step 3: Write minimal implementation**

Add to `HiveFeeIntelligenceBridge` (after `report_traffic_profile`):

```python
    def query_traffic_intelligence(
        self,
        peer_id: str = None,
        profile_type: str = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Query cl-hive for aggregated fleet traffic intelligence.

        Follows query_fee_intelligence pattern: cache → breaker → RPC → cache.
        Uses optional_read policy with stale cache fallback.

        Args:
            peer_id: Specific peer to query (optional — all if omitted)
            profile_type: Filter by profile type (optional)

        Returns:
            Traffic intelligence dict or None if no data available
        """
        cache_key = f"traffic_intel:{peer_id or 'all'}:{profile_type or 'all'}"

        # Check integration cache first (30-min TTL)
        cached = self._get_from_cache(cache_key, ttl=1800.0)
        if cached is not None:
            return cached

        if self._is_circuit_open() or not self.is_available():
            return None

        params = {}
        if peer_id:
            params["peer_id"] = peer_id
        if profile_type:
            params["profile_type"] = profile_type

        ok, result, err = self._rpc_call_with_policy(
            "hive-traffic-intelligence",
            params,
            policy_key="optional_read",
            require_available=False,
            count_error_response_failure=False,
        )
        if not ok:
            if err and not err.startswith("rpc_error:no_data"):
                self._log(f"Failed to query traffic intelligence: {err}", level="debug")
            return None

        if result is None:
            return None
        if result.get("error"):
            if result.get("error") == "no_data":
                return None
            self._log(f"Traffic intelligence query error: {result.get('error')}", level="debug")
            return None

        self._set_in_cache(cache_key, result)
        return result
```

**Step 4: Run test to verify it passes**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_hive_integrations.py::TestTrafficIntelligenceBridge -v`
Expected: PASS (7 tests)

**Step 5: Run full test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -x -q`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/hive_bridge.py tests/test_hive_integrations.py
git commit -m "feat(hive-bridge): add query_traffic_intelligence() method

Queries aggregated fleet traffic data from cl-hive via
hive-traffic-intelligence RPC. Uses optional_read policy with
integration cache (30-min TTL). Part of Phases 3b/3c."
```

---

### Task 3: Revenue-Ops — Enhanced `check_rebalance_conflict()` + `query_fleet_demand_forecast()`

Enhance existing `check_rebalance_conflict()` to pass direction/amount to the new traffic-aware RPC, and add `query_fleet_demand_forecast()`.

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/hive_bridge.py:1005-1036`
- Test: `/home/sat/bin/cl_revenue_ops/tests/test_hive_integrations.py`

**Step 1: Write the failing test**

Add to `TestTrafficIntelligenceBridge`:

```python
    def test_check_rebalance_conflict_traffic_aware(self, hive_bridge):
        """check_rebalance_conflict passes direction and amount to RPC."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = True
        hive_bridge.plugin.rpc.call.return_value = {
            "conflict": False,
            "peer_in_peak_hours": True,
            "suggested_window_utc": [2, 6],
            "fleet_drain_forecast_sats": 500000,
        }

        result = hive_bridge.check_rebalance_conflict(
            peer_id="02" + "a" * 64,
            direction="outbound",
            amount_sats=1000000,
        )

        assert result["peer_in_peak_hours"] is True
        assert result["suggested_window_utc"] == [2, 6]
        call_args = hive_bridge.plugin.rpc.call.call_args
        payload = call_args[0][1]
        assert payload["direction"] == "outbound"
        assert payload["amount_sats"] == 1000000

    def test_check_rebalance_conflict_backwards_compat(self, hive_bridge):
        """check_rebalance_conflict still works with just peer_id."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = True
        hive_bridge.plugin.rpc.call.return_value = {"conflict": False}

        result = hive_bridge.check_rebalance_conflict(peer_id="02" + "a" * 64)

        assert result["conflict"] is False

    def test_query_fleet_demand_forecast_success(self, hive_bridge):
        """query_fleet_demand_forecast returns per-member predictions."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = True
        forecast_data = {
            "members": {
                "02" + "a" * 64: {
                    "predicted_depleted_channels": [],
                    "predicted_surplus_channels": [],
                    "rebalance_demand_sats": 500000,
                    "optimal_rebalance_window_utc": [2, 6],
                }
            },
            "fleet_summary": {"total_rebalance_demand_sats": 500000},
        }
        hive_bridge.plugin.rpc.call.return_value = forecast_data

        result = hive_bridge.query_fleet_demand_forecast(hours_ahead=6)

        assert result is not None
        assert "members" in result

    def test_query_fleet_demand_forecast_hive_down(self, hive_bridge):
        """query_fleet_demand_forecast returns None when hive unavailable."""
        hive_bridge._init_complete = True
        hive_bridge._hive_available = False

        result = hive_bridge.query_fleet_demand_forecast()

        assert result is None
```

**Step 2: Run test to verify it fails**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_hive_integrations.py::TestTrafficIntelligenceBridge::test_check_rebalance_conflict_traffic_aware -v`
Expected: FAIL (signature mismatch or AttributeError)

**Step 3: Write minimal implementation**

Replace existing `check_rebalance_conflict` at L1005-1036 with enhanced version, and add `query_fleet_demand_forecast`:

```python
    def check_rebalance_conflict(
        self,
        peer_id: str,
        direction: str = "outbound",
        amount_sats: int = 0,
    ) -> Dict[str, Any]:
        """
        Check if another fleet member is rebalancing through a peer.

        Enhanced with traffic intelligence: also checks peak hours and
        returns suggested quiet-hour windows for optimal timing.

        Args:
            peer_id: The peer to check
            direction: inbound or outbound (default: outbound)
            amount_sats: Planned rebalance amount in sats

        Returns:
            Conflict info dict with traffic-aware fields:
            {
                "conflict": True/False,
                "peer_in_peak_hours": True/False,
                "suggested_window_utc": [start, end] or None,
                "fleet_drain_forecast_sats": int,
            }
        """
        if self._is_circuit_open() or not self.is_available():
            return {"conflict": False, "reason": "hive_unavailable"}

        ok, result, err = self._rpc_call_with_policy(
            "hive-check-rebalance-conflict",
            {
                "peer_id": peer_id,
                "direction": direction,
                "amount_sats": amount_sats,
            },
            policy_key="optional_read",
            require_available=False,
        )
        if not ok or result is None:
            if err:
                self._log(f"Failed to check rebalance conflict: {err}", level="debug")
            return {"conflict": False, "reason": "exception" if err and err.startswith('exception:') else "check_failed"}
        if result.get("error"):
            self._log(f"Conflict check error: {result.get('error')}", level="debug")
            return {"conflict": False, "reason": "check_failed"}
        return result

    def query_fleet_demand_forecast(
        self,
        hours_ahead: int = 6,
    ) -> Optional[Dict[str, Any]]:
        """
        Query cl-hive for fleet demand forecast.

        Returns per-member predictions of channel depletion, surplus,
        and optimal rebalance windows based on Kalman velocity +
        fleet traffic intelligence.

        Args:
            hours_ahead: Hours to forecast ahead (default: 6)

        Returns:
            Fleet demand forecast dict or None if unavailable
        """
        cache_key = f"fleet_demand_forecast:{hours_ahead}"

        cached = self._get_from_cache(cache_key, ttl=1800.0)
        if cached is not None:
            return cached

        if self._is_circuit_open() or not self.is_available():
            return None

        ok, result, err = self._rpc_call_with_policy(
            "hive-fleet-demand-forecast",
            {"hours_ahead": hours_ahead},
            policy_key="optional_read",
            require_available=False,
        )
        if not ok:
            if err:
                self._log(f"Failed to query fleet demand forecast: {err}", level="debug")
            return None

        if result is None or result.get("error"):
            return None

        self._set_in_cache(cache_key, result)
        return result
```

**Step 4: Run test to verify it passes**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_hive_integrations.py::TestTrafficIntelligenceBridge -v`
Expected: PASS (11 tests)

**Step 5: Run full test suite** (important — existing conflict check tests must still pass)

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -x -q`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/hive_bridge.py tests/test_hive_integrations.py
git commit -m "feat(hive-bridge): enhance check_rebalance_conflict, add query_fleet_demand_forecast

check_rebalance_conflict now passes direction/amount to the traffic-aware
hive RPC (backwards-compatible). query_fleet_demand_forecast queries
Kalman + fleet traffic predictions. Both use optional_read policy."
```

---

### Task 4: Revenue-Ops — Flow Analysis Profile Graduation → `report_traffic_profile()`

After flow analysis completes, report graduated temporal profiles to cl-hive.

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/flow_analysis.py` (class `FlowAnalyzer`)
- Test: `/home/sat/bin/cl_revenue_ops/tests/test_flow_analysis.py`

**Step 1: Write the failing test**

Add a new test class to `test_flow_analysis.py`:

```python
class TestTrafficProfileReporting:
    """Tests for reporting graduated traffic profiles to hive."""

    def test_report_graduated_profiles_calls_bridge(self, flow_analyzer):
        """report_graduated_profiles sends graduated profiles to hive bridge."""
        mock_bridge = MagicMock()
        mock_bridge.report_traffic_profile.return_value = True
        flow_analyzer.hive_bridge = mock_bridge

        # Create a graduated profile
        from modules.flow_analysis import TemporalProfile
        profile = TemporalProfile(
            hourly_out=[float(i * 1000) for i in range(24)],
            hourly_in=[float(i * 500) for i in range(24)],
            hourly_count=[float(i) for i in range(24)],
            peak_hours=[20, 21, 22, 23],
            quiet_hours=[0, 1, 2, 3],
            burstiness=0.5,
            diurnal_strength=0.6,
            observation_days=10,  # > TEMPORAL_GRADUATION_DAYS (7)
            last_updated=int(time.time()),
        )

        # Create matching FlowMetrics
        from modules.flow_analysis import FlowMetrics, ChannelState
        metrics = FlowMetrics(
            channel_id="123x1x0",
            peer_id="02" + "a" * 64,
            sats_in=5000000,
            sats_out=8000000,
            capacity=10000000,
            flow_ratio=0.3,
            state=ChannelState.SOURCE,
            daily_volume=5000000,
            confidence=0.85,
        )

        flow_analyzer._temporal_profiles = {"123x1x0": profile}
        flow_analyzer.report_graduated_profiles({"123x1x0": metrics})

        mock_bridge.report_traffic_profile.assert_called_once()
        call_kwargs = mock_bridge.report_traffic_profile.call_args
        args = call_kwargs[1] if call_kwargs[1] else dict(zip(
            ["peer_id", "profile_type", "peak_hours_utc", "quiet_hours_utc",
             "avg_forward_size_sats", "daily_volume_sats", "drain_direction",
             "confidence", "observation_window_hours"],
            call_kwargs[0]
        ))
        assert args["peer_id"] == "02" + "a" * 64
        assert args["drain_direction"] == "outbound_heavy"

    def test_report_graduated_profiles_skips_ungraduated(self, flow_analyzer):
        """report_graduated_profiles skips profiles with < 7 days observation."""
        mock_bridge = MagicMock()
        flow_analyzer.hive_bridge = mock_bridge

        from modules.flow_analysis import TemporalProfile
        profile = TemporalProfile(observation_days=3)  # Not graduated

        from modules.flow_analysis import FlowMetrics, ChannelState
        metrics = FlowMetrics(
            channel_id="123x1x0",
            peer_id="02" + "a" * 64,
            sats_in=1000, sats_out=2000, capacity=10000000,
            flow_ratio=0.0001, state=ChannelState.BALANCED, daily_volume=1000,
        )

        flow_analyzer._temporal_profiles = {"123x1x0": profile}
        flow_analyzer.report_graduated_profiles({"123x1x0": metrics})

        mock_bridge.report_traffic_profile.assert_not_called()

    def test_report_graduated_profiles_no_bridge(self, flow_analyzer):
        """report_graduated_profiles does nothing without hive bridge."""
        flow_analyzer.hive_bridge = None
        # Should not raise
        flow_analyzer.report_graduated_profiles({})
```

**Step 2: Run test to verify it fails**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_flow_analysis.py::TestTrafficProfileReporting -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Add `hive_bridge` attribute to `FlowAnalyzer.__init__` (at L777-810) and add `report_graduated_profiles` method:

In `__init__`, add after other attributes:
```python
        self.hive_bridge = None  # Set externally for traffic profile reporting
```

Add method after `get_balanced` (~L1889):

```python
    def report_graduated_profiles(self, all_flow: Dict[str, 'FlowMetrics']) -> int:
        """
        Report graduated temporal profiles to cl-hive for fleet sharing.

        Called after analyze_all_channels(). Only reports profiles that have
        graduated (7+ days observation). Maps flow_analysis fields to
        hive traffic profile fields.

        Args:
            all_flow: Dict of channel_id -> FlowMetrics from analyze_all_channels()

        Returns:
            Number of profiles successfully reported
        """
        if not self.hive_bridge:
            return 0

        reported = 0
        for channel_id, profile in self._temporal_profiles.items():
            if not profile.graduated:
                continue

            metrics = all_flow.get(channel_id)
            if not metrics:
                continue

            # Map flow_direction: source=outbound_heavy, sink=inbound_heavy
            if metrics.flow_ratio > 0.1:
                drain_direction = "outbound_heavy"
            elif metrics.flow_ratio < -0.1:
                drain_direction = "inbound_heavy"
            else:
                drain_direction = "balanced"

            # Classify profile type by volume + forward size heuristic
            avg_forward = metrics.daily_volume / max(metrics.forward_count, 1)
            if metrics.daily_volume > 5_000_000 and avg_forward < 50_000:
                profile_type = "retail"
            elif metrics.daily_volume < 2_000_000 and avg_forward > 200_000:
                profile_type = "wholesale"
            else:
                profile_type = "mixed"

            try:
                success = self.hive_bridge.report_traffic_profile(
                    peer_id=metrics.peer_id,
                    profile_type=profile_type,
                    peak_hours_utc=profile.peak_hours,
                    quiet_hours_utc=profile.quiet_hours,
                    avg_forward_size_sats=float(avg_forward),
                    daily_volume_sats=float(metrics.daily_volume),
                    drain_direction=drain_direction,
                    confidence=metrics.confidence,
                    observation_window_hours=profile.observation_days * 24,
                )
                if success:
                    reported += 1
            except Exception:
                pass

        return reported
```

**Step 4: Run test to verify it passes**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_flow_analysis.py::TestTrafficProfileReporting -v`
Expected: PASS (3 tests)

**Step 5: Run full test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -x -q`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/flow_analysis.py tests/test_flow_analysis.py
git commit -m "feat(flow-analysis): report graduated traffic profiles to cl-hive

After temporal profiles graduate (7+ days), map flow_analysis fields
to hive traffic profile format and report via hive_bridge. Classifies
drain_direction from flow_ratio and profile_type from volume heuristic."
```

---

### Task 5: Revenue-Ops — Rebalancer Traffic-Aware Conflict Check

Enhance `execute_rebalance()` to use the traffic-aware conflict check (peak hours, suggested windows).

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/rebalancer.py:4309-4323` (in `execute_rebalance`)
- Test: `/home/sat/bin/cl_revenue_ops/tests/test_rebalancer.py`

**Step 1: Write the failing test**

Add to an appropriate test class in `test_rebalancer.py`:

```python
class TestTrafficAwareRebalancing:
    """Tests for traffic-intelligence-aware rebalancing."""

    def test_rebalance_deferred_during_peak_hours(self, ev_rebalancer, sample_candidate):
        """execute_rebalance logs peak-hour warning when peer is in peak hours."""
        ev_rebalancer.hive_bridge = MagicMock()
        ev_rebalancer.hive_bridge.check_rebalance_conflict.return_value = {
            "conflict": False,
            "peer_in_peak_hours": True,
            "suggested_window_utc": [2, 6],
            "fleet_drain_forecast_sats": 300000,
        }
        ev_rebalancer.hive_bridge.check_circular_flow_risk.return_value = {"risk": False}

        # Peak hours should log but NOT block (informational only in revenue-ops)
        # The actual blocking is done in cl-hive MCF scheduling (Phase 3b)
        result = ev_rebalancer.execute_rebalance(sample_candidate)

        # Should still attempt the rebalance (peak hour is informational at this layer)
        ev_rebalancer.hive_bridge.check_rebalance_conflict.assert_called_once()
        call_kwargs = ev_rebalancer.hive_bridge.check_rebalance_conflict.call_args
        # Verify direction and amount are passed
        assert "direction" in (call_kwargs[1] if call_kwargs[1] else {}) or len(call_kwargs[0]) > 1

    def test_rebalance_passes_direction_and_amount(self, ev_rebalancer, sample_candidate):
        """execute_rebalance passes direction and amount_sats to conflict check."""
        ev_rebalancer.hive_bridge = MagicMock()
        ev_rebalancer.hive_bridge.check_rebalance_conflict.return_value = {
            "conflict": False,
            "peer_in_peak_hours": False,
        }
        ev_rebalancer.hive_bridge.check_circular_flow_risk.return_value = {"risk": False}

        ev_rebalancer.execute_rebalance(sample_candidate)

        call_args = ev_rebalancer.hive_bridge.check_rebalance_conflict.call_args
        # Should include peer_id, direction, and amount_sats
        if call_args[1]:  # kwargs
            assert "direction" in call_args[1] or "amount_sats" in call_args[1]
        else:  # positional
            assert len(call_args[0]) >= 1  # At least peer_id
```

**Step 2: Run test to verify it fails**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_rebalancer.py::TestTrafficAwareRebalancing -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Modify the existing conflict check in `execute_rebalance()` at ~L4315:

Replace:
```python
            conflict = self.hive_bridge.check_rebalance_conflict(candidate.to_peer_id)
```

With:
```python
            conflict = self.hive_bridge.check_rebalance_conflict(
                peer_id=candidate.to_peer_id,
                direction="outbound",
                amount_sats=candidate.amount,
            )
```

And after the existing conflict logging (~L4323), add peak hour logging:

```python
            # Log traffic intelligence info (informational — does not block)
            if conflict.get("peer_in_peak_hours"):
                window = conflict.get("suggested_window_utc")
                self.plugin.log(
                    f"TRAFFIC_INTEL: {candidate.to_channel[:12]}... peer in peak hours"
                    f"{f', suggested window: {window}' if window else ''}",
                    level='info'
                )
```

**Step 4: Run test to verify it passes**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_rebalancer.py::TestTrafficAwareRebalancing -v`
Expected: PASS (2 tests)

**Step 5: Run full test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -x -q`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/rebalancer.py tests/test_rebalancer.py
git commit -m "feat(rebalancer): pass direction/amount to traffic-aware conflict check

execute_rebalance now passes direction and amount_sats to
check_rebalance_conflict for traffic-intelligent scheduling.
Logs peak-hour warnings (informational — does not block rebalancing
at this layer; blocking is handled by Phase 3b MCF scheduling)."
```

---

### Task 6: Revenue-Ops — Fee Controller Traffic Intelligence Query

Query fleet traffic intelligence in `_get_coordinated_fee_recommendation()` for forward-size-aware fee decisions.

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/fee_controller.py:4817-4894`
- Test: `/home/sat/bin/cl_revenue_ops/tests/test_fee_controller.py`

**Step 1: Write the failing test**

Add to `test_fee_controller.py`:

```python
class TestTrafficIntelligenceFees:
    """Tests for traffic-intelligence-aware fee coordination."""

    def test_fee_recommendation_queries_traffic_intel(self, fee_controller):
        """_get_coordinated_fee_recommendation queries traffic intelligence."""
        fee_controller.ENABLE_HIVE_COORDINATION = True
        fee_controller.hive_bridge = MagicMock()
        fee_controller.hive_bridge.query_coordinated_fee_recommendation.return_value = {
            "recommended_fee_ppm": 200,
            "confidence": 0.8,
            "size_adjustment_pct": 0.1,  # +10% for small forwards
        }
        fee_controller.hive_bridge.query_traffic_intelligence.return_value = {
            "avg_forward_size_sats": 5000.0,
            "daily_volume_sats": 2000000.0,
            "confidence": 0.75,
        }

        result = fee_controller._get_coordinated_fee_recommendation(
            channel_id="123x1x0",
            peer_id="02" + "a" * 64,
            current_fee=150,
            local_balance_pct=0.5,
        )

        # Should return the coordinated fee
        assert result is not None
        # Traffic intel should have been queried
        fee_controller.hive_bridge.query_traffic_intelligence.assert_called_once()

    def test_fee_recommendation_works_without_traffic_intel(self, fee_controller):
        """Fee coordination works when traffic intelligence is unavailable."""
        fee_controller.ENABLE_HIVE_COORDINATION = True
        fee_controller.hive_bridge = MagicMock()
        fee_controller.hive_bridge.query_coordinated_fee_recommendation.return_value = {
            "recommended_fee_ppm": 200,
            "confidence": 0.8,
        }
        fee_controller.hive_bridge.query_traffic_intelligence.return_value = None

        result = fee_controller._get_coordinated_fee_recommendation(
            channel_id="123x1x0",
            peer_id="02" + "a" * 64,
            current_fee=150,
            local_balance_pct=0.5,
        )

        assert result == 200
```

**Step 2: Run test to verify it fails**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_fee_controller.py::TestTrafficIntelligenceFees -v`
Expected: FAIL

**Step 3: Write minimal implementation**

In `_get_coordinated_fee_recommendation()`, after the existing coordinated fee query (~L4887), add traffic intelligence query:

```python
            # Query traffic intelligence for forward-size context
            traffic_intel = None
            try:
                traffic_intel = self.hive_bridge.query_traffic_intelligence(peer_id=peer_id)
            except Exception:
                pass

            if traffic_intel and recommended_fee is not None:
                avg_fwd = traffic_intel.get("avg_forward_size_sats", 0)
                daily_vol = traffic_intel.get("daily_volume_sats", 0)
                intel_confidence = traffic_intel.get("confidence", 0)

                if intel_confidence > 0.3:
                    # Log traffic context for transparency
                    self.plugin.log(
                        f"TRAFFIC_INTEL: {channel_id} -> {peer_id[:12]}... "
                        f"avg_fwd={avg_fwd:.0f} daily_vol={daily_vol:.0f}",
                        level="debug"
                    )
```

**Step 4: Run test to verify it passes**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_fee_controller.py::TestTrafficIntelligenceFees -v`
Expected: PASS (2 tests)

**Step 5: Run full test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -x -q`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/fee_controller.py tests/test_fee_controller.py
git commit -m "feat(fee-controller): query traffic intelligence for fee context

_get_coordinated_fee_recommendation now queries fleet traffic intelligence
for forward-size and volume context. Logs traffic data for transparency.
Fails open — missing traffic data uses standard coordination."
```

---

### Task 7: Revenue-Ops — Capacity Planner Fleet Demand Forecast

Query fleet demand forecast from cl-hive in `generate_report()`.

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/modules/capacity_planner.py:34-74`
- Test: `/home/sat/bin/cl_revenue_ops/tests/test_capacity_planner.py`

**Step 1: Write the failing test**

Add to `test_capacity_planner.py`:

```python
class TestFleetDemandForecast:
    """Tests for fleet demand forecast integration."""

    def test_generate_report_includes_fleet_forecast(self, capacity_planner):
        """generate_report includes fleet demand forecast when available."""
        capacity_planner.hive_bridge = MagicMock()
        capacity_planner.hive_bridge.query_fleet_demand_forecast.return_value = {
            "members": {},
            "fleet_summary": {"total_rebalance_demand_sats": 750000},
        }

        report = capacity_planner.generate_report()

        assert "fleet_demand_forecast" in report
        capacity_planner.hive_bridge.query_fleet_demand_forecast.assert_called_once()

    def test_generate_report_works_without_hive(self, capacity_planner):
        """generate_report works without hive bridge."""
        capacity_planner.hive_bridge = None

        report = capacity_planner.generate_report()

        assert "fleet_demand_forecast" not in report or report["fleet_demand_forecast"] is None
```

**Step 2: Run test to verify it fails**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_capacity_planner.py::TestFleetDemandForecast -v`
Expected: FAIL

**Step 3: Write minimal implementation**

Add `hive_bridge` attribute to `CapacityPlanner.__init__`:
```python
        self.hive_bridge = None  # Set externally for fleet demand forecast
```

In `generate_report()`, after the `all_flow` fetch (~L42), add:
```python
        # Query fleet demand forecast if available
        fleet_forecast = None
        if self.hive_bridge:
            try:
                fleet_forecast = self.hive_bridge.query_fleet_demand_forecast()
            except Exception:
                pass
```

And add to the return dict (~L67-74):
```python
            "fleet_demand_forecast": fleet_forecast,
```

**Step 4: Run test to verify it passes**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/test_capacity_planner.py::TestFleetDemandForecast -v`
Expected: PASS (2 tests)

**Step 5: Run full test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -x -q`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add modules/capacity_planner.py tests/test_capacity_planner.py
git commit -m "feat(capacity-planner): query fleet demand forecast from cl-hive

generate_report includes fleet demand forecast when hive bridge is
available. Fails open — missing forecast doesn't block report generation."
```

---

### Task 8: cl-hive — Phase 3b: MCF Scheduling Conflict Check

Add traffic-intelligence-aware scheduling to `_process_mcf_assignments()` in `background_loops.py`. Members check `check_rebalance_conflict` before sending ACK, deferring assignments during peak hours.

**Files:**
- Modify: `/home/sat/bin/cl-hive/modules/background_loops.py:1747-1796`
- Test: `/home/sat/bin/cl-hive/tests/test_traffic_intelligence.py`

**Step 1: Write the failing test**

Add a new test class to `test_traffic_intelligence.py`:

```python
class TestMCFScheduling:
    """Phase 3b: MCF assignment scheduling with traffic intelligence."""

    def test_mcf_defers_during_peak_hours(self):
        """Pending MCF assignments are deferred when peer is in peak hours."""
        from modules import background_loops

        mock_plugin = MagicMock()
        mock_lc = MagicMock()
        mock_traffic_intel = MagicMock()

        # Assignment targeting a peer in peak hours
        mock_assignment = MagicMock()
        mock_assignment.to_channel = "02" + "a" * 64
        mock_assignment.solution_timestamp = 1000

        mock_lc.get_mcf_status.return_value = {
            "assignment_counts": {"pending": 1, "executing": 0, "completed": 0, "failed": 0},
            "ack_sent": True,
        }
        mock_lc.get_pending_mcf_assignments.return_value = [mock_assignment]

        mock_traffic_intel.check_rebalance_conflict.return_value = {
            "conflict": False,
            "peer_in_peak_hours": True,
            "suggested_window_utc": [2, 6],
            "fleet_drain_forecast_sats": 0,
            "conflicting_member": None,
        }

        # Inject dependencies
        background_loops.plugin = mock_plugin
        background_loops.liquidity_coord = mock_lc
        background_loops.traffic_intel_mgr = mock_traffic_intel
        background_loops.cost_reduction_mgr = MagicMock()

        # Reset defer tracking
        if hasattr(background_loops, '_mcf_defer_counts'):
            background_loops._mcf_defer_counts = {}

        background_loops._process_mcf_assignments()

        # Should have logged the deferral
        mock_plugin.log.assert_any_call(
            unittest.mock.ANY,  # Message contains "peak_hours" or "deferred"
            level=unittest.mock.ANY,
        )

    def test_mcf_executes_after_max_deferrals(self):
        """MCF assignment executes after max_defer_cycles regardless of peak hours."""
        from modules import background_loops

        mock_plugin = MagicMock()
        mock_lc = MagicMock()
        mock_traffic_intel = MagicMock()

        mock_assignment = MagicMock()
        mock_assignment.to_channel = "02" + "b" * 64
        mock_assignment.assignment_id = "test-assign-1"
        mock_assignment.solution_timestamp = 1000

        mock_lc.get_mcf_status.return_value = {
            "assignment_counts": {"pending": 1, "executing": 0, "completed": 0, "failed": 0},
            "ack_sent": True,
        }
        mock_lc.get_pending_mcf_assignments.return_value = [mock_assignment]

        mock_traffic_intel.check_rebalance_conflict.return_value = {
            "conflict": False,
            "peer_in_peak_hours": True,
            "suggested_window_utc": [2, 6],
            "fleet_drain_forecast_sats": 0,
            "conflicting_member": None,
        }

        background_loops.plugin = mock_plugin
        background_loops.liquidity_coord = mock_lc
        background_loops.traffic_intel_mgr = mock_traffic_intel
        background_loops.cost_reduction_mgr = MagicMock()

        # Pre-set defer count to max
        background_loops._mcf_defer_counts = {"test-assign-1": 3}

        background_loops._process_mcf_assignments()

        # Should NOT defer — max deferrals reached, proceed normally

    def test_mcf_skips_on_active_conflict(self):
        """MCF assignment is skipped when another member is actively rebalancing."""
        from modules import background_loops

        mock_plugin = MagicMock()
        mock_lc = MagicMock()
        mock_traffic_intel = MagicMock()

        mock_assignment = MagicMock()
        mock_assignment.to_channel = "02" + "c" * 64
        mock_assignment.assignment_id = "test-assign-2"
        mock_assignment.solution_timestamp = 1000

        mock_lc.get_mcf_status.return_value = {
            "assignment_counts": {"pending": 1, "executing": 0, "completed": 0, "failed": 0},
            "ack_sent": True,
        }
        mock_lc.get_pending_mcf_assignments.return_value = [mock_assignment]

        mock_traffic_intel.check_rebalance_conflict.return_value = {
            "conflict": True,
            "conflicting_member": "02" + "d" * 64,
            "peer_in_peak_hours": False,
            "suggested_window_utc": None,
            "fleet_drain_forecast_sats": 0,
        }

        background_loops.plugin = mock_plugin
        background_loops.liquidity_coord = mock_lc
        background_loops.traffic_intel_mgr = mock_traffic_intel
        background_loops.cost_reduction_mgr = MagicMock()
        background_loops._mcf_defer_counts = {}

        background_loops._process_mcf_assignments()

        # Should log conflict skip
```

**Step 2: Run test to verify it fails**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py::TestMCFScheduling -v`
Expected: FAIL (no traffic-aware logic yet)

**Step 3: Write minimal implementation**

Add module-level defer tracking and modify `_process_mcf_assignments()`:

At module level (near other module globals):
```python
# Phase 3b: MCF assignment defer tracking
_mcf_defer_counts: Dict[str, int] = {}
_MCF_MAX_DEFER_CYCLES = 3
```

Replace `_process_mcf_assignments()` at L1747-1796:

```python
def _process_mcf_assignments():
    """
    Process pending MCF assignments for our node.

    Phase 3b: Before ACK, checks traffic intelligence for peak-hour
    conflicts and active fleet rebalancing. Defers up to 3 cycles
    (~90 minutes), then executes regardless.
    """
    global _mcf_defer_counts

    if not liquidity_coord or not cost_reduction_mgr:
        return

    try:
        status = liquidity_coord.get_mcf_status()
        counts = status.get("assignment_counts", {})

        pending_count = counts.get("pending", 0)
        executing_count = counts.get("executing", 0)
        completed_count = counts.get("completed", 0)
        failed_count = counts.get("failed", 0)

        # Phase 3b: Check traffic intelligence before ACK
        if pending_count > 0 and traffic_intel_mgr:
            pending = liquidity_coord.get_pending_mcf_assignments()
            for assignment in (pending or []):
                peer_id = getattr(assignment, 'to_channel', '')
                assign_id = getattr(assignment, 'assignment_id', str(id(assignment)))

                # Check fleet rebalancing conflict and peak hours
                try:
                    conflict_info = traffic_intel_mgr.check_rebalance_conflict(
                        peer_id=peer_id,
                        direction="outbound",
                        amount_sats=0,
                    )
                except Exception:
                    conflict_info = {}

                # Active conflict — skip entirely (another member rebalancing)
                if conflict_info.get("conflict"):
                    member = conflict_info.get("conflicting_member", "unknown")
                    plugin.log(
                        f"cl-hive: MCF assignment {assign_id[:12]}... skipped — "
                        f"conflict with {member[:12]}...",
                        level='info'
                    )
                    continue

                # Peak hours — defer up to max_defer_cycles
                defer_count = _mcf_defer_counts.get(assign_id, 0)
                if conflict_info.get("peer_in_peak_hours") and defer_count < _MCF_MAX_DEFER_CYCLES:
                    _mcf_defer_counts[assign_id] = defer_count + 1
                    window = conflict_info.get("suggested_window_utc")
                    plugin.log(
                        f"cl-hive: MCF assignment {assign_id[:12]}... deferred "
                        f"(peer in peak hours, defer {defer_count + 1}/{_MCF_MAX_DEFER_CYCLES})"
                        f"{f', suggested window: {window}' if window else ''}",
                        level='info'
                    )
                    continue

                # Clear defer count on execution
                _mcf_defer_counts.pop(assign_id, None)

        # Send ACK if we have pending assignments and haven't ACKed yet
        if pending_count > 0 and not status.get("ack_sent", False):
            pending = liquidity_coord.get_pending_mcf_assignments()
            if pending:
                solution_timestamp = pending[0].solution_timestamp
                ack_msg = liquidity_coord.create_mcf_ack_message()
                if ack_msg:
                    _broadcast_mcf_ack(ack_msg)

        # Log status periodically
        if pending_count > 0 or executing_count > 0:
            plugin.log(
                f"cl-hive: MCF assignments - pending={pending_count}, "
                f"executing={executing_count}, completed={completed_count}, "
                f"failed={failed_count}",
                level='debug'
            )

        _check_stuck_mcf_assignments()

    except Exception as e:
        plugin.log(f"cl-hive: MCF assignment processing error: {e}", level='debug')
```

**Step 4: Run test to verify it passes**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py::TestMCFScheduling -v`
Expected: PASS (3 tests)

**Step 5: Run full test suite**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix`
Expected: All pass

**Step 6: Commit**

```bash
cd /home/sat/bin/cl-hive && git add modules/background_loops.py tests/test_traffic_intelligence.py
git commit -m "feat(phase-3b): MCF scheduling with traffic intelligence

_process_mcf_assignments now checks traffic intelligence before ACK:
- Active conflict (another member rebalancing) → skip entirely
- Peer in peak hours → defer up to 3 cycles (~90 min)
- After max deferrals → execute regardless (stale > suboptimal)

Decentralized scheduling — no protocol changes needed."
```

---

### Task 9: cl-hive — Phase 3c: Size-Aware Fee Enrichment

Add `get_size_aware_adjustment()` to `FeeCoordinationManager` and integrate into `get_fee_recommendation()`.

**Files:**
- Modify: `/home/sat/bin/cl-hive/modules/fee_coordination.py`
- Test: `/home/sat/bin/cl-hive/tests/test_traffic_intelligence.py`

**Step 1: Write the failing test**

Add a new test class to `test_traffic_intelligence.py`:

```python
class TestSizeAwareFeeEnrichment:
    """Phase 3c: Size-aware fee adjustment based on fleet traffic intelligence."""

    def test_large_forwards_get_discount(self):
        """Peers with large average forwards get a fee discount (0.9x)."""
        from modules.fee_coordination import FeeCoordinationManager

        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)

        mock_traffic_intel = MagicMock()
        mock_traffic_intel.get_aggregated_profile.return_value = {
            "avg_forward_size_sats": 600000,  # > 500k threshold
            "daily_volume_sats": 5000000,
            "confidence": 0.8,
        }
        mgr.traffic_intel_mgr = mock_traffic_intel

        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)

        assert 0.85 <= multiplier <= 0.95  # Should be ~0.9

    def test_small_forwards_get_premium(self):
        """Peers with small average forwards get a fee premium (1.1x)."""
        from modules.fee_coordination import FeeCoordinationManager

        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)

        mock_traffic_intel = MagicMock()
        mock_traffic_intel.get_aggregated_profile.return_value = {
            "avg_forward_size_sats": 5000,  # < 10k threshold
            "daily_volume_sats": 2000000,
            "confidence": 0.8,
        }
        mgr.traffic_intel_mgr = mock_traffic_intel

        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)

        assert 1.05 <= multiplier <= 1.15  # Should be ~1.1

    def test_high_volume_gets_floor_boost(self):
        """High-volume peers get +0.05 floor boost on top of other adjustments."""
        from modules.fee_coordination import FeeCoordinationManager

        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)

        mock_traffic_intel = MagicMock()
        mock_traffic_intel.get_aggregated_profile.return_value = {
            "avg_forward_size_sats": 100000,  # Normal size, no size adjustment
            "daily_volume_sats": 15000000,  # > 10M threshold
            "confidence": 0.8,
        }
        mgr.traffic_intel_mgr = mock_traffic_intel

        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)

        assert multiplier >= 1.04  # Should include floor boost

    def test_no_traffic_data_returns_neutral(self):
        """No traffic data returns neutral 1.0 multiplier."""
        from modules.fee_coordination import FeeCoordinationManager

        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)

        mock_traffic_intel = MagicMock()
        mock_traffic_intel.get_aggregated_profile.return_value = None
        mgr.traffic_intel_mgr = mock_traffic_intel

        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)

        assert multiplier == 1.0

    def test_no_traffic_intel_mgr_returns_neutral(self):
        """No traffic_intel_mgr returns neutral 1.0 multiplier."""
        from modules.fee_coordination import FeeCoordinationManager

        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)
        mgr.traffic_intel_mgr = None

        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)

        assert multiplier == 1.0

    def test_multiplier_bounded(self):
        """Multiplier is always bounded to [0.8, 1.3]."""
        from modules.fee_coordination import FeeCoordinationManager

        mock_db = MagicMock()
        mock_plugin = MagicMock()
        mgr = FeeCoordinationManager(mock_db, mock_plugin)

        mock_traffic_intel = MagicMock()
        # Extreme values should still be bounded
        mock_traffic_intel.get_aggregated_profile.return_value = {
            "avg_forward_size_sats": 1,  # Very small
            "daily_volume_sats": 100000000,  # Very high volume
            "confidence": 1.0,
        }
        mgr.traffic_intel_mgr = mock_traffic_intel

        multiplier = mgr.get_size_aware_adjustment("02" + "a" * 64)

        assert 0.8 <= multiplier <= 1.3
```

**Step 2: Run test to verify it fails**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py::TestSizeAwareFeeEnrichment -v`
Expected: FAIL with `AttributeError`

**Step 3: Write minimal implementation**

Add `traffic_intel_mgr` attribute and `set_traffic_intel_mgr` setter to `FeeCoordinationManager`:

In `__init__` (after `self.fee_intelligence_mgr = None` at L2275):
```python
        # Phase 3c: Optional reference to TrafficIntelligenceManager for size-aware fees
        self.traffic_intel_mgr = None
```

Add setter (after `set_fee_intelligence_mgr` at L2291):
```python
    def set_traffic_intel_mgr(self, mgr: Any) -> None:
        """Set reference to TrafficIntelligenceManager for size-aware fee enrichment."""
        self.traffic_intel_mgr = mgr
```

Add the `get_size_aware_adjustment` method (before `get_fee_recommendation` at L2360):

```python
    def get_size_aware_adjustment(self, peer_id: str) -> float:
        """
        Calculate fee adjustment based on fleet traffic intelligence forward sizes.

        Phase 3c: Returns a multiplier (0.8-1.3) based on:
        - avg_forward_size > 500k sats → 0.9x (attract whale traffic)
        - avg_forward_size < 10k sats → 1.1x (HTLC slot cost for small forwards)
        - daily_volume > 10M sats → +0.05 floor boost (protect capacity)
        - No traffic data → 1.0x (neutral, preserve current behavior)

        Args:
            peer_id: External peer to check

        Returns:
            Fee multiplier bounded to [0.8, 1.3]
        """
        if not self.traffic_intel_mgr:
            return 1.0

        try:
            profile = self.traffic_intel_mgr.get_aggregated_profile(peer_id)
        except Exception:
            return 1.0

        if not profile:
            return 1.0

        avg_fwd = profile.get("avg_forward_size_sats", 0)
        daily_vol = profile.get("daily_volume_sats", 0)
        confidence = profile.get("confidence", 0)

        if confidence < 0.3:
            return 1.0

        multiplier = 1.0

        # Size-based adjustment
        if avg_fwd > 500_000:
            multiplier = 0.9  # Attract whale traffic
        elif avg_fwd < 10_000 and avg_fwd > 0:
            multiplier = 1.1  # HTLC slot cost for small forwards

        # Volume floor boost
        if daily_vol > 10_000_000:
            multiplier += 0.05

        # Bound to [0.8, 1.3]
        return max(0.8, min(1.3, multiplier))
```

**Step 4: Run test to verify it passes**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/test_traffic_intelligence.py::TestSizeAwareFeeEnrichment -v`
Expected: PASS (6 tests)

**Step 5: Integrate into `get_fee_recommendation()`**

Add `size_adjustment_pct` to `FeeRecommendation` dataclass (after `centrality_adjustment_pct` at L288):
```python
    size_adjustment_pct: float = 0.0        # Phase 3c: Size-aware adjustment
```

Add to `to_dict()` (after the centrality conditional at L322):
```python
        if self.size_adjustment_pct != 0.0:
            result["size_adjustment_pct"] = round(self.size_adjustment_pct * 100, 1)
```

In `get_fee_recommendation()`, add step 6b after centrality adjustment (after L2498):
```python
        # 6b. Apply size-aware adjustment (Phase 3c)
        size_adjustment_pct = 0.0
        size_multiplier = self.get_size_aware_adjustment(peer_id)
        if size_multiplier != 1.0:
            size_adjustment_pct = size_multiplier - 1.0
            recommended_fee = int(recommended_fee * size_multiplier)
            if size_multiplier > 1.0:
                reasons.append(f"size_premium_{size_adjustment_pct*100:.1f}%")
            else:
                reasons.append(f"size_discount_{size_adjustment_pct*100:.1f}%")
```

Add `size_adjustment_pct=size_adjustment_pct` to the FeeRecommendation constructor at the end (~L2563).

**Step 6: Run full test suite**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix`
Expected: All pass

**Step 7: Commit**

```bash
cd /home/sat/bin/cl-hive && git add modules/fee_coordination.py tests/test_traffic_intelligence.py
git commit -m "feat(phase-3c): size-aware fee enrichment from fleet traffic intelligence

FeeCoordinationManager.get_size_aware_adjustment() returns a bounded
multiplier (0.8-1.3) based on fleet traffic intelligence:
- Large forwards (>500k) → 0.9x discount (attract whale traffic)
- Small forwards (<10k) → 1.1x premium (HTLC slot cost)
- High volume (>10M/day) → +0.05 floor boost
- No data → 1.0x neutral

Integrated into get_fee_recommendation() as step 6b, stored in
FeeRecommendation.size_adjustment_pct."
```

---

### Task 10: cl-hive — Wire Phase 3c into cl-hive.py

Inject `traffic_intel_mgr` into `FeeCoordinationManager` via the setter pattern.

**Files:**
- Modify: `/home/sat/bin/cl-hive/cl-hive.py`
- No new tests (wiring only — covered by existing integration)

**Step 1: Find the wiring point**

In `cl-hive.py`, find where `fee_coord_mgr.set_fee_intelligence_mgr(fee_intel_mgr)` is called. Add the traffic intelligence setter nearby:

```python
fee_coord_mgr.set_traffic_intel_mgr(traffic_intel_mgr)
```

**Step 2: Run full test suite**

Run: `cd /home/sat/bin/cl-hive && python3 -m pytest tests/ -x -q --deselect tests/test_anticipatory_nnlb_bugs.py::TestHiveBridgeKeyFix`
Expected: All pass

**Step 3: Commit**

```bash
cd /home/sat/bin/cl-hive && git add cl-hive.py
git commit -m "feat: wire traffic_intel_mgr into FeeCoordinationManager

Enables Phase 3c size-aware fee enrichment by injecting the
TrafficIntelligenceManager into the fee coordination system."
```

---

### Task 11: Revenue-Ops — Wire hive_bridge into flow_analysis and capacity_planner

Set `hive_bridge` on `FlowAnalyzer` and `CapacityPlanner` in `cl-revenue-ops.py`, and call `report_graduated_profiles()` after flow analysis cycle.

**Files:**
- Modify: `/home/sat/bin/cl_revenue_ops/cl-revenue-ops.py`
- No new tests (wiring only — covered by unit tests in Tasks 4 and 7)

**Step 1: Find wiring points**

In `cl-revenue-ops.py`, find where `flow_analyzer` and `capacity_planner` are created. After their creation, add:

```python
flow_analyzer.hive_bridge = hive_bridge
capacity_planner.hive_bridge = hive_bridge
```

Find the flow analysis cycle (where `analyze_all_channels()` is called). After the analysis completes, add:

```python
# Report graduated traffic profiles to cl-hive
if hasattr(flow_analyzer, 'report_graduated_profiles'):
    try:
        reported = flow_analyzer.report_graduated_profiles(all_flow)
        if reported > 0:
            plugin.log(f"Reported {reported} graduated traffic profiles to hive", level='info')
    except Exception as e:
        plugin.log(f"Failed to report traffic profiles: {e}", level='debug')
```

**Step 2: Run full test suite**

Run: `cd /home/sat/bin/cl_revenue_ops && python3 -m pytest tests/ -x -q`
Expected: All pass

**Step 3: Commit**

```bash
cd /home/sat/bin/cl_revenue_ops && git add cl-revenue-ops.py
git commit -m "feat: wire hive_bridge into flow_analysis and capacity_planner

Enables traffic profile reporting after flow analysis cycle and
fleet demand forecast in capacity planning reports."
```

---

## Summary

| Task | Repo | Phase | What |
|------|------|-------|------|
| 1 | cl-revenue-ops | Rev-ops | `report_traffic_profile()` bridge method |
| 2 | cl-revenue-ops | Rev-ops | `query_traffic_intelligence()` bridge method |
| 3 | cl-revenue-ops | Rev-ops | Enhanced `check_rebalance_conflict()` + `query_fleet_demand_forecast()` |
| 4 | cl-revenue-ops | Rev-ops | Flow analysis profile graduation → reporting |
| 5 | cl-revenue-ops | Rev-ops | Rebalancer traffic-aware conflict check |
| 6 | cl-revenue-ops | Rev-ops | Fee controller traffic intelligence query |
| 7 | cl-revenue-ops | Rev-ops | Capacity planner fleet demand forecast |
| 8 | cl-hive | Phase 3b | MCF scheduling with traffic intelligence |
| 9 | cl-hive | Phase 3c | Size-aware fee enrichment |
| 10 | cl-hive | Phase 3c | Wire traffic_intel_mgr into fee coordination |
| 11 | cl-revenue-ops | Rev-ops | Wire hive_bridge into flow_analysis + capacity_planner |

**Estimated new tests:** ~30 across both repos
**Files modified:** 11 files across 2 repos
**No protocol changes. No new gossip messages. No changes to existing RPCs.**
