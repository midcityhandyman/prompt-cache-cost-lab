"""Turn recorded requests into billable usage under each strategy, then cost them.

A recorded request is what the workload sent with no optimisation at all:
a stable prefix (system prompt, tool definitions, reference material), a
dynamic tail (the question for this request) and the output it produced.
Each scenario re-bills those same requests a different way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .pricing import Cost, PriceBook, Usage, total_cost


@dataclass(frozen=True)
class Request:
    id: str
    t: float  # seconds since the first request started
    prefix_tokens: int
    dynamic_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ScenarioResult:
    key: str
    name: str
    model_label: str
    cost: Cost
    requests: int
    cache_hits: int
    tradeoff: str


def load_requests(path: str | Path) -> list[Request]:
    """Read a JSONL usage fixture. Lines starting with # are comments."""
    requests = []
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
                request = Request(
                    id=str(row["id"]),
                    t=float(row["t"]),
                    prefix_tokens=int(row["prefix_tokens"]),
                    dynamic_tokens=int(row["dynamic_tokens"]),
                    output_tokens=int(row["output_tokens"]),
                )
            except (KeyError, TypeError, ValueError) as problem:
                raise ValueError(f"{path}:{number}: bad usage record ({problem})") from None
            if min(request.prefix_tokens, request.dynamic_tokens, request.output_tokens) < 0:
                raise ValueError(f"{path}:{number}: token counts cannot be negative")
            requests.append(request)
    if not requests:
        raise ValueError(f"{path}: no usage records found")
    return sorted(requests, key=lambda r: r.t)


def bill_uncached(requests: list[Request]) -> list[Usage]:
    return [
        Usage(input_tokens=r.prefix_tokens + r.dynamic_tokens, output_tokens=r.output_tokens)
        for r in requests
    ]


def bill_cached(
    requests: list[Request], ttl_seconds: float, min_prefix_tokens: int, one_hour: bool = False
) -> tuple[list[Usage], int]:
    """Replay the requests against a simulated prefix cache.

    The first request writes the prefix. A later request reads it if it starts
    within `ttl_seconds` of the last write or read (a read refreshes the timer),
    otherwise it writes again. Returns the usage list and the number of hits.
    """
    usages: list[Usage] = []
    hits = 0
    last_touch: float | None = None
    for r in requests:
        if r.prefix_tokens < min_prefix_tokens:
            usages.extend(bill_uncached([r]))
            continue
        warm = last_touch is not None and (r.t - last_touch) < ttl_seconds
        last_touch = r.t
        if warm:
            hits += 1
            usages.append(
                Usage(
                    input_tokens=r.dynamic_tokens,
                    cache_read_tokens=r.prefix_tokens,
                    output_tokens=r.output_tokens,
                )
            )
        else:
            write = {"cache_write_1h_tokens" if one_hour else "cache_write_5m_tokens": r.prefix_tokens}
            usages.append(Usage(input_tokens=r.dynamic_tokens, output_tokens=r.output_tokens, **write))
    return usages, hits


def run_scenarios(
    requests: list[Request], book: PriceBook, baseline_model: str, cheaper_model: str
) -> list[ScenarioResult]:
    """Cost the workload every way. The first result is always the baseline."""
    base = book.model(baseline_model)
    cheap = book.model(cheaper_model)
    n = len(requests)
    plain = bill_uncached(requests)
    cached_5m, hits_5m = bill_cached(requests, book.ttl_5m_seconds, book.min_cacheable_prefix_tokens)
    cached_1h, hits_1h = bill_cached(
        requests, book.ttl_1h_seconds, book.min_cacheable_prefix_tokens, one_hour=True
    )

    def result(key, name, model, usages, hits, tradeoff, multiplier=1.0):
        return ScenarioResult(key, name, model.label, total_cost(usages, model, multiplier), n, hits, tradeoff)

    return [
        result("baseline", "No caching (baseline)", base, plain, 0, "none"),
        result("cache_5m", "Prompt caching, 5 minute TTL", base, cached_5m, hits_5m,
               "prefix must stay byte identical"),
        result("cache_1h", "Prompt caching, 1 hour TTL", base, cached_1h, hits_1h,
               "writes cost more, survives idle gaps"),
        result("batch", "Batch API, no caching", base, plain, 0,
               "asynchronous, not for interactive use", book.batch_multiplier),
        result("cheaper", "Cheaper tier, no caching", cheap, plain, 0,
               "answer quality must be re-checked"),
        result("cheaper_cache_5m", "Cheaper tier + 5 minute caching", cheap, cached_5m, hits_5m,
               "both of the above"),
    ]
