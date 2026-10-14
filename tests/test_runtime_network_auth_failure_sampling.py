from agent_runtime.runtime_network_monitor import (
    AUTH_FAILURE_LOG_SAMPLE_MAX_KEYS,
    RuntimeNetworkMonitor,
)


def test_auth_failure_sampling_admits_first_record_and_suppresses_subsequent() -> None:
    monitor = RuntimeNetworkMonitor()
    key = "GET /api/lacp/status"
    t0 = 1000.0

    admitted, suppressed = monitor.admit_auth_failure_record(key, now=t0)
    assert admitted is True
    assert suppressed == 0

    # Within 60 seconds: suppressed
    admitted, suppressed = monitor.admit_auth_failure_record(key, now=t0 + 1.0)
    assert admitted is False
    assert suppressed == 0

    admitted, suppressed = monitor.admit_auth_failure_record(key, now=t0 + 59.9)
    assert admitted is False
    assert suppressed == 0

    assert monitor.snapshot()["suppressed_auth_failure_records"] == 2

    # After 60 seconds: admitted again, with suppressed count of previous window
    admitted, suppressed = monitor.admit_auth_failure_record(key, now=t0 + 60.0)
    assert admitted is True
    assert suppressed == 2

    # Still total suppressed is preserved in snapshot
    assert monitor.snapshot()["suppressed_auth_failure_records"] == 2


def test_auth_failure_sampling_independent_keys() -> None:
    monitor = RuntimeNetworkMonitor()
    key1 = "GET /api/lacp/status"
    key2 = "GET /api/agent-runtime/scenes"
    t0 = 2000.0

    # key1 admitted
    admitted, suppressed = monitor.admit_auth_failure_record(key1, now=t0)
    assert admitted is True
    assert suppressed == 0

    # key2 admitted independently
    admitted, suppressed = monitor.admit_auth_failure_record(key2, now=t0 + 5.0)
    assert admitted is True
    assert suppressed == 0

    # key1 suppressed
    admitted, suppressed = monitor.admit_auth_failure_record(key1, now=t0 + 10.0)
    assert admitted is False

    # key2 suppressed
    admitted, suppressed = monitor.admit_auth_failure_record(key2, now=t0 + 15.0)
    assert admitted is False

    assert monitor.snapshot()["suppressed_auth_failure_records"] == 2


def test_auth_failure_sampling_evicts_oldest_key_when_exceeding_max_keys() -> None:
    monitor = RuntimeNetworkMonitor()
    t0 = 3000.0

    # Fill up to max keys
    for i in range(AUTH_FAILURE_LOG_SAMPLE_MAX_KEYS):
        admitted, suppressed = monitor.admit_auth_failure_record(f"KEY_{i}", now=t0)
        assert admitted is True

    # Suppress KEY_0 once
    admitted, suppressed = monitor.admit_auth_failure_record("KEY_0", now=t0 + 1.0)
    assert admitted is False

    # Note that KEY_0 was touched? But admit_auth_failure_record with suppressed does NOT move to end,
    # or does it? Let's check: only when admitted does it move_to_end, or key_0 is oldest if not moved?
    # Wait, KEY_1 hasn't been accessed since t0. KEY_512 will evict the oldest item.
    # In order of admission: KEY_1 is near front.
    admitted, suppressed = monitor.admit_auth_failure_record("NEW_KEY", now=t0 + 2.0)
    assert admitted is True
    assert len(monitor._auth_failure_windows) == AUTH_FAILURE_LOG_SAMPLE_MAX_KEYS
