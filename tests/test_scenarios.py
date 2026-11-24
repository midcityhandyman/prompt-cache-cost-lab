import pytest

from costlab.pricing import Usage
from costlab.scenarios import Request, bill_cached, bill_uncached, load_requests, run_scenarios
from test_pricing import TEST_PRICES, book  # noqa: F401  (fixture)


def req(t, prefix=1000, dynamic=100, output=50, id="r"):
    return Request(id=id, t=t, prefix_tokens=prefix, dynamic_tokens=dynamic, output_tokens=output)


def test_uncached_bills_prefix_and_tail_as_plain_input():
    assert bill_uncached([req(0)]) == [Usage(input_tokens=1100, output_tokens=50)]


def test_first_request_writes_then_warm_requests_read():
    usages, hits = bill_cached([req(0), req(60), req(120)], ttl_seconds=300, min_prefix_tokens=500)
    assert usages[0] == Usage(input_tokens=100, cache_write_5m_tokens=1000, output_tokens=50)
    assert usages[1] == Usage(input_tokens=100, cache_read_tokens=1000, output_tokens=50)
    assert hits == 2


def test_gap_longer_than_ttl_forces_a_rewrite():
    usages, hits = bill_cached([req(0), req(301)], ttl_seconds=300, min_prefix_tokens=500)
    assert usages[1].cache_write_5m_tokens == 1000 and hits == 0


def test_reads_refresh_the_ttl():
    # 0 -> 250 -> 500: each gap is under 300s even though 500 is past the first write's TTL.
    _, hits = bill_cached([req(0), req(250), req(500)], ttl_seconds=300, min_prefix_tokens=500)
    assert hits == 2


def test_prefix_below_minimum_never_caches():
    usages, hits = bill_cached([req(0, prefix=400), req(10, prefix=400)], 300, min_prefix_tokens=500)
    assert hits == 0 and all(u.cache_read_tokens == 0 and u.cache_write_5m_tokens == 0 for u in usages)


def test_one_hour_writes_use_the_one_hour_line():
    usages, _ = bill_cached([req(0)], 3600, 500, one_hour=True)
    assert usages[0].cache_write_1h_tokens == 1000 and usages[0].cache_write_5m_tokens == 0


def test_scenarios_match_hand_computed_totals(book):  # noqa: F811
    # Two requests 60s apart, then one after a 20 minute gap. Model "big": 10 in, 50 out.
    requests = [req(0), req(60), req(60 + 1200)]
    results = {r.key: r for r in run_scenarios(requests, book, "big", "small")}

    baseline = (3 * 1100 * 10 + 3 * 50 * 50) / 1e6
    assert results["baseline"].cost.total == pytest.approx(baseline)

    # 5m: write, read, write (gap too long).
    cache_5m = (3 * 100 * 10 + 2 * 1000 * 12.5 + 1 * 1000 * 1.0 + 3 * 50 * 50) / 1e6
    assert results["cache_5m"].cost.total == pytest.approx(cache_5m)
    assert results["cache_5m"].cache_hits == 1

    # 1h: write, read, read.
    cache_1h = (3 * 100 * 10 + 1 * 1000 * 20.0 + 2 * 1000 * 1.0 + 3 * 50 * 50) / 1e6
    assert results["cache_1h"].cost.total == pytest.approx(cache_1h)
    assert results["cache_1h"].cache_hits == 2

    assert results["batch"].cost.total == pytest.approx(baseline / 2)
    # "small" long prompt tier kicks in above 1000 prompt tokens: 2 in, 10 out.
    assert results["cheaper"].cost.total == pytest.approx((3 * 1100 * 2 + 3 * 50 * 10) / 1e6)


def test_baseline_is_first_and_single_request_caching_costs_more(book):  # noqa: F811
    results = run_scenarios([req(0)], book, "big", "big")
    assert results[0].key == "baseline"
    by_key = {r.key: r for r in results}
    assert by_key["cache_5m"].cost.total > by_key["baseline"].cost.total


def test_load_requests_skips_comments_sorts_and_reports_bad_lines(tmp_path):
    good = tmp_path / "usage.jsonl"
    good.write_text(
        '# comment\n{"id":"b","t":5,"prefix_tokens":1,"dynamic_tokens":2,"output_tokens":3}\n'
        '{"id":"a","t":0,"prefix_tokens":1,"dynamic_tokens":2,"output_tokens":3}\n',
        encoding="utf-8",
    )
    assert [r.id for r in load_requests(good)] == ["a", "b"]
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"id":"a","t":0}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="bad.jsonl:1"):
        load_requests(bad)
