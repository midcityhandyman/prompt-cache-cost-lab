"""Replay a small prompt set and measure usage, with caching off and then on.

Two transports share one interface:

- MockTransport: offline. Estimates tokens from text length and simulates the
  prefix cache. Used by the tests, the demo and any run without a key.
- AnthropicTransport: sends real requests through the official SDK and reads
  the usage the API reports. Only built when the caller asks for a live run.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from .pricing import Cost, PriceBook, Usage, total_cost

MAX_OUTPUT_TOKENS = 1024


class ReplayError(RuntimeError):
    """A live request failed in a way the user should read, not a traceback."""


@dataclass(frozen=True)
class PromptSet:
    system: str
    questions: list[str]


@dataclass(frozen=True)
class ReplayResult:
    mode: str  # "mock" or "live"
    model_label: str
    uncached: list[Usage]
    cached: list[Usage]
    cost_uncached: Cost
    cost_cached: Cost


class Transport(Protocol):
    mode: str

    def send(self, model: str, system: str, question: str, cache: bool) -> Usage: ...


def load_prompts(path: str | Path) -> PromptSet:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return PromptSet(system=raw["system"], questions=list(raw["questions"]))


def load_env_file(path: str | Path = ".env") -> None:
    """Minimal .env reader: KEY=value lines, existing environment wins."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def estimate_tokens(text: str) -> int:
    """Rough offline estimate (about four characters per token). Not a tokenizer."""
    return max(1, len(text) // 4)


class MockTransport:
    """Deterministic stand in for the API. Every request is `step_seconds` apart."""

    mode = "mock"

    def __init__(self, ttl_seconds: float = 300, min_prefix_tokens: int = 512, step_seconds: float = 20):
        self.ttl_seconds = ttl_seconds
        self.min_prefix_tokens = min_prefix_tokens
        self.step_seconds = step_seconds
        self.clock = 0.0
        self._cache: dict[tuple[str, str], float] = {}  # (model, system) -> last touch

    def send(self, model: str, system: str, question: str, cache: bool) -> Usage:
        self.clock += self.step_seconds
        prefix = estimate_tokens(system)
        tail = estimate_tokens(question)
        output = 90 + (len(question) * 7) % 160
        if not cache or prefix < self.min_prefix_tokens:
            return Usage(input_tokens=prefix + tail, output_tokens=output)
        key = (model, system)
        last = self._cache.get(key)
        self._cache[key] = self.clock
        if last is not None and self.clock - last < self.ttl_seconds:
            return Usage(input_tokens=tail, cache_read_tokens=prefix, output_tokens=output)
        return Usage(input_tokens=tail, cache_write_5m_tokens=prefix, output_tokens=output)


class AnthropicTransport:
    """Real requests. Costs real money. Needs `pip install -e ".[live]"` and a key."""

    mode = "live"

    def __init__(self) -> None:
        try:
            import anthropic
        except ImportError:
            raise ReplayError('Live replay needs the SDK: pip install -e ".[live]"') from None
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()

    def send(self, model: str, system: str, question: str, cache: bool) -> Usage:
        anthropic = self._anthropic
        system_block = {"type": "text", "text": system}
        if cache:
            # Breakpoint at the end of the stable prefix. The question comes after it.
            system_block["cache_control"] = {"type": "ephemeral"}
        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                output_config={"effort": "low"},
                system=[system_block],
                messages=[{"role": "user", "content": question}],
            )
        except anthropic.AuthenticationError:
            raise ReplayError("The API rejected the key. Check ANTHROPIC_API_KEY.") from None
        except anthropic.RateLimitError:
            raise ReplayError("Rate limited after retries. Wait and run the replay again.") from None
        except anthropic.APIStatusError as error:
            raise ReplayError(f"API error {error.status_code}: {error.message}") from None
        except anthropic.APIConnectionError:
            raise ReplayError("Could not reach the API. Check the network.") from None
        if response.stop_reason == "refusal":
            raise ReplayError("The model declined a replay prompt. Usage for it is not comparable.")
        usage = response.usage
        return Usage(
            input_tokens=usage.input_tokens,
            cache_write_5m_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            output_tokens=usage.output_tokens,
        )


def pick_transport(live: bool, book: PriceBook) -> tuple[Transport, str]:
    """Live only when asked for AND a key is present. Otherwise the mock, with the reason."""
    if not live:
        return _mock(book), "offline mock (pass --live to use the real API)"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _mock(book), "offline mock (--live was passed but ANTHROPIC_API_KEY is not set)"
    return AnthropicTransport(), "LIVE API (real requests, real cost)"


def _mock(book: PriceBook) -> MockTransport:
    return MockTransport(book.ttl_5m_seconds, book.min_cacheable_prefix_tokens)


def run_replay(prompts: PromptSet, transport: Transport, book: PriceBook, model_id: str) -> ReplayResult:
    """Send every question twice: first with caching off, then with caching on."""
    model = book.model(model_id)  # fail on unknown or placeholder prices before spending anything
    uncached = [transport.send(model_id, prompts.system, q, cache=False) for q in prompts.questions]
    cached = [transport.send(model_id, prompts.system, q, cache=True) for q in prompts.questions]
    return ReplayResult(
        mode=transport.mode,
        model_label=model.label,
        uncached=uncached,
        cached=cached,
        cost_uncached=total_cost(uncached, model),
        cost_cached=total_cost(cached, model),
    )


def save_usage(result: ReplayResult, path: str | Path) -> None:
    """Write per request usage as JSONL so a run can be inspected later.

    Every row carries `mode` ("live" or "mock"), so mock estimates cannot be
    mistaken for measured usage once the file is on disk.
    """
    with open(path, "w", encoding="utf-8") as handle:
        for label, usages in (("uncached", result.uncached), ("cached", result.cached)):
            for index, usage in enumerate(usages):
                handle.write(json.dumps({"mode": result.mode, "pass": label, "request": index, **asdict(usage)}) + "\n")


def replay_report(result: ReplayResult, book: PriceBook, transport_note: str) -> str:
    from .report import _table, money, price_header

    def row(name: str, usages: list[Usage], cost: Cost) -> list[str]:
        return [
            name,
            f"{sum(u.input_tokens for u in usages):,}",
            f"{sum(u.cache_write_5m_tokens for u in usages):,}",
            f"{sum(u.cache_read_tokens for u in usages):,}",
            f"{sum(u.output_tokens for u in usages):,}",
            money(cost.total),
        ]

    table = _table(
        ["Pass", "Input tok", "Cache write tok", "Cache read tok", "Output tok", "Cost"],
        [
            row("Before: caching off", result.uncached, result.cost_uncached),
            row("After: caching on", result.cached, result.cost_cached),
        ],
    )
    before, after = result.cost_uncached.total, result.cost_cached.total
    percent = ((before - after) / before * 100) if before else 0.0
    lines = [
        f"Replay transport: {transport_note}",
        f"Model: {result.model_label}, {len(result.uncached)} requests per pass",
        price_header(book),
        "",
        table,
        "",
        f"Saved by caching on this replay: {money(before - after)} ({percent:.1f}%)",
    ]
    if result.mode == "mock":
        lines.append("Token counts above are mock estimates (about 4 characters per token), not measurements.")
    elif not any(u.cache_read_tokens for u in result.cached):
        lines.append(
            "WARNING: no cache reads were reported. The prefix may be below the model's "
            "cacheable minimum, or something in it changes between requests."
        )
    return "\n".join(lines)
