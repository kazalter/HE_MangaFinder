import json
import logging

from app.core.observability import capture_metrics, record_metric, sanitize_fields, set_metric_attr


def test_observability_captures_nested_counters_and_redacts_secrets(caplog):
    with caplog.at_level(logging.INFO, logger="app.observability"):
        with capture_metrics("outer", log_summary=True) as outer:
            set_metric_attr("cookie", "secret-cookie-value")
            set_metric_attr("x-csrf-token", "csrf-123")
            set_metric_attr("authorization", "Bearer secret")
            set_metric_attr("scan_mode", "recent")
            with capture_metrics("inner"):
                record_metric("list_requests", 2)
                record_metric("detail_requests", 3)
            record_metric("aggregation_executed", 1)

    snap = outer.snapshot()
    assert snap["counters"] == {
        "list_requests": 2,
        "detail_requests": 3,
        "aggregation_executed": 1,
    }
    assert snap["attributes"] == {"scan_mode": "recent"}
    assert "cookie" not in json.dumps(snap)
    assert "csrf-123" not in caplog.text
    assert "Bearer secret" not in caplog.text
    assert "metrics.summary" in caplog.text


def test_sanitize_fields_strips_credential_keys():
    cleaned = sanitize_fields(
        {
            "scan_mode": "deep",
            "api_key": "sk-123",
            "session_state": "{}",
            "pages_covered": 4,
        }
    )
    assert cleaned == {"scan_mode": "deep", "pages_covered": 4}
