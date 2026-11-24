from pathlib import Path

import json

import pytest

from costlab import cli
from costlab.pricing import load_prices
from costlab.report import money
from costlab.replay import MockTransport, PromptSet, load_prompts, pick_transport, run_replay, save_usage

ROOT = Path(__file__).resolve().parents[1]
PRICES = ROOT / "config" / "prices.toml"


@pytest.fixture
def book():
    return load_prices(PRICES)


def test_mock_replay_first_cached_request_writes_and_the_rest_read(book):
    prompts = load_prompts(ROOT / "fixtures" / "replay_prompts.json")
    result = run_replay(prompts, MockTransport(), book, "claude-opus-5-5")
    assert result.mode == "mock"
    assert all(u.cache_read_tokens == 0 and u.cache_write_5m_tokens == 0 for u in result.uncached)
    assert result.cached[0].cache_write_5m_tokens > 0
    assert all(u.cache_read_tokens > 0 for u in result.cached[1:])
    assert result.cost_cached.total < result.cost_uncached.total


def test_mock_cache_expires_and_ignores_short_prefixes():
    slow = MockTransport(ttl_seconds=300, min_prefix_tokens=10, step_seconds=400)
    system = "x" * 400
    assert slow.send("m", system, "q", cache=True).cache_write_5m_tokens == 100
    assert slow.send("m", system, "q", cache=True).cache_write_5m_tokens == 100  # expired
    short = MockTransport(min_prefix_tokens=512)
    assert short.send("m", "tiny", "q", cache=True).cache_write_5m_tokens == 0


def test_cache_is_scoped_to_model_and_prefix():
    mock = MockTransport(min_prefix_tokens=10)
    system = "y" * 400
    mock.send("a", system, "q", cache=True)
    assert mock.send("b", system, "q", cache=True).cache_read_tokens == 0
    assert mock.send("a", system + "!", "q", cache=True).cache_read_tokens == 0
    assert mock.send("a", system, "q", cache=True).cache_read_tokens == 100


def test_live_is_never_picked_without_flag_or_key(book, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    transport, note = pick_transport(live=False, book=book)
    assert transport.mode == "mock"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    transport, note = pick_transport(live=True, book=book)
    assert transport.mode == "mock" and "not set" in note


def test_replay_refuses_unpriced_model_before_sending(book):
    class Exploding:
        mode = "mock"

        def send(self, *args, **kwargs):
            raise AssertionError("must not send")

    with pytest.raises(ValueError):
        run_replay(PromptSet("s", ["q"]), Exploding(), book, "no-such-model")


def test_save_usage_writes_one_line_per_request(book, tmp_path):
    result = run_replay(PromptSet("s" * 4000, ["a", "b"]), MockTransport(), book, "claude-sonnet-5-5")
    out = tmp_path / "usage.jsonl"
    save_usage(result, out)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 4
    # Mock estimates must stay labelled once they are on disk.
    assert {row["mode"] for row in rows} == {"mock"}


def test_cli_prices_file_with_missing_key_reports_error(tmp_path, capsys):
    broken = tmp_path / "prices.toml"
    broken.write_text(PRICES.read_text(encoding="utf-8").replace('fetched_on = "', 'fetched = "'), encoding="utf-8")
    assert cli.main(["--prices", str(broken), "report"]) == 1
    assert "missing key" in capsys.readouterr().err


def test_money_puts_the_minus_sign_before_the_dollar_sign():
    assert money(1234.5) == "$1,234.5000"
    assert money(-0.0062) == "-$0.0062"
    assert money(-0.00001) == "$0.0000"


def test_cli_report_prints_before_and_after_table(capsys):
    assert cli.main(["report"]) == 0
    out = capsys.readouterr().out
    assert "BEFORE AND AFTER" in out and "Fetched on:" in out
    assert "Prompt caching, 5 minute TTL" in out and "Batch API" in out and "Cheaper tier" in out


def test_cli_replay_runs_offline(capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cli, "load_env_file", lambda *_: None)
    assert cli.main(["replay", "--live"]) == 0
    assert "offline mock" in capsys.readouterr().out


def test_cli_reports_errors_without_traceback(capsys):
    assert cli.main(["report", "--baseline", "no-such-model"]) == 1
    assert "error:" in capsys.readouterr().err


def test_repo_has_no_em_dashes():
    skip = {".venv", ".git", "__pycache__", ".pytest_cache"}
    for path in ROOT.rglob("*"):
        if path.is_file() and not skip.intersection(path.parts) and path.suffix in {
            ".py", ".md", ".toml", ".json", ".jsonl", ".yml", ".txt", ".example"
        }:
            assert chr(0x2014) not in path.read_text(encoding="utf-8"), path
