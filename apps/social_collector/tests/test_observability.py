from app.observability import capture_metrics, record_metric


def test_collector_observability_scopes_counters():
    with capture_metrics() as scoped:
        record_metric("graphql_calls", 2)
        record_metric("bytes_transferred", 512)
        record_metric("cookie_value", 99)

    snap = scoped.snapshot()
    assert snap["counters"]["graphql_calls"] == 2
    assert snap["counters"]["bytes_transferred"] == 512
    assert "cookie_value" not in snap["counters"]
    assert snap["memory"]["peak_rss_kib"] >= 0
