# Cairn

A microframework for compute graphs with caching, tracing, and replay.

Think **PyTorch for agent pipelines** — though nothing about it is agent-specific. You write async Python; a `@step` decorator turns each function into a tracked, cached node in a graph that emerges from execution rather than being declared up front.

> ⚠️ **Alpha.** The API is unstable and will change without notice. No semver guarantees. Not on PyPI. Pin to a commit if you depend on it.

## Why

Declarative graph frameworks (LangGraph, CrewAI, Airflow-style DAGs) force you into their node/edge DSL. Cairn does the opposite: the graph is your code, the framework just instruments it. From that you get caching keyed on function identity + version + inputs, a live trace of execution, restartable pipelines, and replay with simulated timing — all from ordinary `async def`.

Works for agent pipelines, scrapers, ETL, or anything expressible as pure-ish async functions.

## Install

```sh
uv pip install -e .
# or with extras
uv pip install -e ".[tui,pydantic]"
```

Requires Python 3.12+.

## Example

```python
import asyncio
from cairn import step, run, trace

@step(memo=True)
async def fetch(url: str) -> str:
    trace("fetching", url=url)
    ...  # actual fetch
    return html

@step
async def extract(html: str) -> list[str]:
    return [...]

async def pipeline(urls: list[str]):
    pages = await asyncio.gather(*[fetch(u) for u in urls])
    return await asyncio.gather(*[extract(p) for p in pages])

run(lambda: pipeline(["https://a", "https://b"]))
```

Second run is instant — every `@step(memo=True)` result is cached by `(function version, inputs)`. Change one function's body and only its downstream re-executes.

See `examples/` for scrapers, research pipelines, failure/resume, and human-in-the-loop patterns.

## Docs

- [`docs/motivation.md`](docs/motivation.md) — why this exists, what problem it solves
- [`docs/design.md`](docs/design.md) — core abstractions (`@step`, `Handle`, `trace`, stores)
- [`docs/patterns.md`](docs/patterns.md) — composable patterns (retry, validation loops, fan-out)

## Status

Alpha. Core primitives work and tests pass, but:
- Public API names may still change (including the `@step` decorator itself).
- On-disk cache format is not stable across versions.
- No published releases — install from source.
- Feedback and breakage reports welcome via issues.
