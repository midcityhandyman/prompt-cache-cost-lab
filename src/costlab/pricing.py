"""Price book loading and the one function that turns token usage into dollars.

All prices come from config/prices.toml. No price is hard coded in Python.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER = "PLACEHOLDER"
RATE_FIELDS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
MTOK = 1_000_000


class PriceConfigError(ValueError):
    """The price file is malformed."""


class PlaceholderPriceError(PriceConfigError):
    """A model was requested whose prices are still placeholders."""


@dataclass(frozen=True)
class Rates:
    """USD per million tokens for each billing line."""

    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float


@dataclass(frozen=True)
class ModelPrices:
    model_id: str
    label: str
    standard: Rates
    long_prompt_over: int | None = None
    long_prompt: Rates | None = None

    def rates_for(self, prompt_tokens: int) -> Rates:
        """Pick the rate card for one request, based on its total prompt length."""
        if self.long_prompt and self.long_prompt_over is not None:
            if prompt_tokens > self.long_prompt_over:
                return self.long_prompt
        return self.standard


@dataclass(frozen=True)
class Usage:
    """Billable tokens for one request, split the way the API reports them."""

    input_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.cache_read_tokens
        )


@dataclass(frozen=True)
class Cost:
    """Dollars for one request or a sum of requests."""

    input: float = 0.0
    cache_write: float = 0.0
    cache_read: float = 0.0
    output: float = 0.0

    @property
    def total(self) -> float:
        return self.input + self.cache_write + self.cache_read + self.output

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(
            self.input + other.input,
            self.cache_write + other.cache_write,
            self.cache_read + other.cache_read,
            self.output + other.output,
        )


@dataclass(frozen=True)
class PriceBook:
    source_url: str
    fetched_on: str
    batch_multiplier: float
    ttl_5m_seconds: int
    ttl_1h_seconds: int
    min_cacheable_prefix_tokens: int
    models: dict[str, ModelPrices]
    placeholder_models: frozenset[str]

    def model(self, model_id: str) -> ModelPrices:
        if model_id in self.placeholder_models:
            raise PlaceholderPriceError(
                f"{model_id} still has {PLACEHOLDER} prices in the price file. "
                f"Fill them in from {self.source_url} before costing it."
            )
        try:
            return self.models[model_id]
        except KeyError:
            known = ", ".join(sorted(self.models) or ["none"])
            raise PriceConfigError(f"No prices for {model_id}. Known models: {known}") from None


def _rates(table: dict, where: str) -> Rates | None:
    """Return Rates, or None when any field is a placeholder."""
    values = {}
    for field in RATE_FIELDS:
        if field not in table:
            raise PriceConfigError(f"{where}: missing price '{field}'")
        value = table[field]
        if value == PLACEHOLDER:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise PriceConfigError(f"{where}: '{field}' must be a number >= 0 or \"{PLACEHOLDER}\"")
        values[field] = float(value)
    return Rates(**values)


def load_prices(path: str | Path) -> PriceBook:
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    try:
        meta, modifiers, caching = raw["meta"], raw["modifiers"], raw["caching"]
        model_tables = raw["models"]
    except KeyError as missing:
        raise PriceConfigError(f"price file is missing section {missing}") from None

    try:
        models: dict[str, ModelPrices] = {}
        placeholders: set[str] = set()
        for model_id, table in model_tables.items():
            standard = _rates(table, model_id)
            long_table = table.get("long_prompt")
            long_rates = _rates(long_table, f"{model_id}.long_prompt") if long_table else None
            if standard is None or (long_table and long_rates is None):
                placeholders.add(model_id)
                continue
            models[model_id] = ModelPrices(
                model_id=model_id,
                label=table.get("label", model_id),
                standard=standard,
                long_prompt_over=int(long_table["over_prompt_tokens"]) if long_table else None,
                long_prompt=long_rates,
            )

        return PriceBook(
            source_url=meta["source_url"],
            fetched_on=str(meta["fetched_on"]),
            batch_multiplier=float(modifiers["batch_multiplier"]),
            ttl_5m_seconds=int(caching["ttl_5m_seconds"]),
            ttl_1h_seconds=int(caching["ttl_1h_seconds"]),
            min_cacheable_prefix_tokens=int(caching["min_cacheable_prefix_tokens"]),
            models=models,
            placeholder_models=frozenset(placeholders),
        )
    except KeyError as missing:
        # A missing key anywhere in the file is a config problem, not a crash.
        raise PriceConfigError(f"price file is missing key {missing}") from None


def cost_of(usage: Usage, model: ModelPrices, multiplier: float = 1.0) -> Cost:
    """Price one request. `multiplier` carries the batch discount (1.0 means none)."""
    rates = model.rates_for(usage.prompt_tokens)
    scale = multiplier / MTOK
    return Cost(
        input=usage.input_tokens * rates.input * scale,
        cache_write=(
            usage.cache_write_5m_tokens * rates.cache_write_5m
            + usage.cache_write_1h_tokens * rates.cache_write_1h
        )
        * scale,
        cache_read=usage.cache_read_tokens * rates.cache_read * scale,
        output=usage.output_tokens * rates.output * scale,
    )


def total_cost(usages: list[Usage], model: ModelPrices, multiplier: float = 1.0) -> Cost:
    total = Cost()
    for usage in usages:
        total = total + cost_of(usage, model, multiplier)
    return total
