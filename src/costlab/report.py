"""Plain text tables. No dependencies, stable output so it can be diffed in CI."""

from __future__ import annotations

from .pricing import PriceBook
from .scenarios import ScenarioResult


def _table(headers: list[str], rows: list[list[str]], left: int = 1) -> str:
    """Render rows with the first `left` columns left aligned, the rest right aligned."""
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]

    def line(cells):
        parts = [
            str(cell).ljust(width) if index < left else str(cell).rjust(width)
            for index, (cell, width) in enumerate(zip(cells, widths))
        ]
        return "  ".join(parts).rstrip()

    rule = "  ".join("-" * width for width in widths)
    return "\n".join([line(headers), rule, *(line(row) for row in rows)])


def money(value: float) -> str:
    """Dollars to four places. A loss reads -$0.0062, and a tiny one never prints as -$0.0000."""
    text = f"{abs(value):,.4f}"
    return f"-${text}" if value < 0 and float(text.replace(",", "")) else f"${text}"


def price_header(book: PriceBook) -> str:
    return (
        f"Prices: {book.source_url}\n"
        f"Fetched on: {book.fetched_on} (re-check before relying on these numbers)"
    )


def before_after_table(results: list[ScenarioResult]) -> str:
    """One row per strategy: cost before (baseline), cost after, and the saving."""
    before = results[0].cost.total
    rows = []
    for r in results[1:]:
        after = r.cost.total
        saved = before - after
        percent = (saved / before * 100) if before else 0.0
        rows.append([r.name, r.model_label, money(before), money(after), money(saved), f"{percent:.1f}%"])
    return _table(["Strategy", "Model", "Before", "After", "Saved", "Saved %"], rows, left=2)


def breakdown_table(results: list[ScenarioResult]) -> str:
    """Where the dollars go in each scenario, including the baseline."""
    rows = [
        [
            r.name,
            money(r.cost.input),
            money(r.cost.cache_write),
            money(r.cost.cache_read),
            money(r.cost.output),
            money(r.cost.total),
            f"{r.cache_hits}/{r.requests}",
        ]
        for r in results
    ]
    return _table(
        ["Scenario", "Input", "Cache write", "Cache read", "Output", "Total", "Cache hits"], rows
    )


def tradeoff_list(results: list[ScenarioResult]) -> str:
    return "\n".join(f"- {r.name}: {r.tradeoff}" for r in results[1:])


def full_report(results: list[ScenarioResult], book: PriceBook, source: str) -> str:
    return "\n\n".join(
        [
            f"Workload: {source} ({results[0].requests} requests)\n{price_header(book)}",
            "BEFORE AND AFTER\n" + before_after_table(results),
            "COST BREAKDOWN\n" + breakdown_table(results),
            "WHAT EACH STRATEGY COSTS YOU\n" + tradeoff_list(results),
        ]
    )
