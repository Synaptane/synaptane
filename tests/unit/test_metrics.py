"""Prometheus text format: escaping, numbers, cumulative histograms, areas, token length."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from synaptane.config import Settings
from synaptane.core.metrics import BUCKETS, Exposition, Histogram, Requests, area


def test_labels_are_escaped_and_numbers_written_plainly() -> None:
    out = Exposition()
    out.add("x", "gauge", 'Help with "quotes"\nand a line', [({"space": 'a"b\\c\nd'}, 2.0)])
    out.one("y", "gauge", "Half.", 0.5)
    out.one("z", "gauge", "Infinite.", float("inf"))
    assert out.text().splitlines() == [
        '# HELP x Help with \\"quotes\\"\\nand a line',
        "# TYPE x gauge",
        'x{space="a\\"b\\\\c\\nd"} 2',
        "# HELP y Half.",
        "# TYPE y gauge",
        "y 0.5",
        "# HELP z Infinite.",
        "# TYPE z gauge",
        "z +Inf",
    ]


def test_histogram_buckets_are_cumulative() -> None:
    histogram = Histogram()
    for seconds in (0.005, 0.01, 0.3, 100):
        histogram.observe(seconds)
    out = Exposition()
    out.histogram("d", "Duration.", [({"area": "web"}, histogram)])
    lines = out.text().splitlines()
    assert 'd_bucket{area="web",le="0.01"} 2' in lines  # 0.01 counts in le="0.01"
    assert 'd_bucket{area="web",le="0.25"} 2' in lines
    assert 'd_bucket{area="web",le="0.5"} 3' in lines
    assert 'd_bucket{area="web",le="30"} 3' in lines
    assert 'd_bucket{area="web",le="+Inf"} 4' in lines
    assert 'd_count{area="web"} 4' in lines
    assert 'd_sum{area="web"} 100.315' in lines
    assert sum(1 for line in lines if line.startswith("d_bucket")) == len(BUCKETS) + 1


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/mcp", "mcp"),
        ("/mcp/", "mcp"),
        ("/mcpx", "web"),
        ("/api/v1/search", "api"),
        ("/static/app.css", "static"),
        ("/token", "oauth"),
        ("/.well-known/oauth-authorization-server", "oauth"),
        ("/o/acme/infra", "web"),
    ],
)
def test_area_of_a_request(path: str, expected: str) -> None:
    assert area(path) == expected


def test_requests_by_status_class() -> None:
    requests = Requests()
    requests.record("/", 200, 0.1)
    requests.record("/", 302, 0.1)
    requests.record("/api/v1/me", 401, 0.1)
    out = Exposition()
    requests.write(out)
    lines = out.text().splitlines()
    assert 'synaptane_http_requests_total{area="web",code="2xx"} 1' in lines
    assert 'synaptane_http_requests_total{area="web",code="3xx"} 1' in lines
    assert 'synaptane_http_requests_total{area="api",code="4xx"} 1' in lines


def test_a_metrics_token_must_be_long(tmp_path: Path) -> None:
    def settings(token: str) -> Settings:
        return Settings(
            database_url="postgresql+psycopg://x@localhost/x",
            data_dir=tmp_path,
            metrics_token=token,
        )

    with pytest.raises(ValidationError, match="32 characters"):
        settings("secret")
    assert settings("").metrics_token == ""
    assert settings(" " + "a" * 32 + " ").metrics_token == "a" * 32
