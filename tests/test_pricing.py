from pathlib import Path

import pytest

from costlab.pricing import PlaceholderPriceError, PriceConfigError, Usage, cost_of, load_prices

ROOT = Path(__file__).resolve().parents[1]

TEST_PRICES = """
[meta]
source_url = "https://example.com/pricing"
fetched_on = "2000-01-01"
[modifiers]
batch_multiplier = 0.5
[caching]
ttl_5m_seconds = 300
ttl_1h_seconds = 3600
min_cacheable_prefix_tokens = 500
[models.big]
label = "Big"
input = 10.0
cache_write_5m = 12.5
cache_write_1h = 20.0
cache_read = 1.0
output = 50.0
[models.small]
label = "Small"
input = 1.0
cache_write_5m = 1.25
cache_write_1h = 2.0
cache_read = 0.1
output = 5.0
[models.small.long_prompt]
over_prompt_tokens = 1000
input = 2.0
cache_write_5m = 2.5
cache_write_1h = 4.0
cache_read = 0.2
output = 10.0
[models.unknown]
label = "Not filled in"
input = "PLACEHOLDER"
cache_write_5m = "PLACEHOLDER"
cache_write_1h = "PLACEHOLDER"
cache_read = "PLACEHOLDER"
output = "PLACEHOLDER"
"""


@pytest.fixture
def book(tmp_path):
    path = tmp_path / "prices.toml"
    path.write_text(TEST_PRICES, encoding="utf-8")
    return load_prices(path)


def test_each_billing_line_is_priced_per_million(book):
    usage = Usage(
        input_tokens=1_000_000,
        cache_write_5m_tokens=1_000_000,
        cache_write_1h_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    cost = cost_of(usage, book.model("big"))
    assert cost.input == pytest.approx(10.0)
    assert cost.cache_write == pytest.approx(12.5 + 20.0)
    assert cost.cache_read == pytest.approx(1.0)
    assert cost.output == pytest.approx(50.0)
    assert cost.total == pytest.approx(93.5)


def test_batch_multiplier_halves_everything(book):
    usage = Usage(input_tokens=200_000, output_tokens=100_000)
    full = cost_of(usage, book.model("big"))
    batch = cost_of(usage, book.model("big"), book.batch_multiplier)
    assert batch.total == pytest.approx(full.total / 2)


def test_long_prompt_tier_applies_per_request_and_counts_cache_tokens(book):
    small = book.model("small")
    at_threshold = Usage(input_tokens=1000, output_tokens=100)
    assert cost_of(at_threshold, small).input == pytest.approx(1000 * 1.0 / 1e6)
    # 900 cached + 101 fresh = 1001 prompt tokens, over the threshold.
    over = Usage(input_tokens=101, cache_read_tokens=900, output_tokens=100)
    cost = cost_of(over, small)
    assert cost.input == pytest.approx(101 * 2.0 / 1e6)
    assert cost.cache_read == pytest.approx(900 * 0.2 / 1e6)
    assert cost.output == pytest.approx(100 * 10.0 / 1e6)


def test_placeholder_model_is_refused_not_guessed(book):
    assert "unknown" in book.placeholder_models
    with pytest.raises(PlaceholderPriceError, match="PLACEHOLDER"):
        book.model("unknown")


def test_unknown_model_lists_known_ones(book):
    with pytest.raises(PriceConfigError, match="big"):
        book.model("nope")


def test_bad_price_value_is_rejected(tmp_path):
    path = tmp_path / "prices.toml"
    path.write_text(TEST_PRICES.replace("input = 10.0", "input = -1"), encoding="utf-8")
    with pytest.raises(PriceConfigError, match="input"):
        load_prices(path)


def test_shipped_price_file_loads_and_records_its_source():
    book = load_prices(ROOT / "config" / "prices.toml")
    assert book.source_url.startswith("https://")
    assert len(book.fetched_on) == 10
    for model_id in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        rates = book.model(model_id).standard
        # Shape checks only, so a price update does not break the suite.
        assert rates.cache_read < rates.input < rates.cache_write_5m < rates.cache_write_1h
        assert rates.output > rates.input


def test_no_prices_hard_coded_in_source():
    """Dollar figures belong in config/prices.toml only."""
    for path in (ROOT / "src" / "costlab").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "per_mtok" not in text and "$4" not in text and "20.00" not in text, path.name


@pytest.mark.parametrize(
    "removed",
    ['fetched_on = "2000-01-01"', "ttl_1h_seconds = 3600", "over_prompt_tokens = 1000"],
)
def test_missing_key_is_a_config_error_not_a_keyerror(tmp_path, removed):
    assert removed in TEST_PRICES
    path = tmp_path / "prices.toml"
    path.write_text(TEST_PRICES.replace(removed, ""), encoding="utf-8")
    with pytest.raises(PriceConfigError, match="missing key"):
        load_prices(path)
