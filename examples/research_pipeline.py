"""Research pipeline example.

Demonstrates: fan-out, retry loop, caching, rate limiting, API failures, trace annotations.
Uses a fake Claude API with realistic failure modes.
"""

from __future__ import annotations

import asyncio
import hashlib
import random

from cairn import step, run, trace
from cairn import rate_limited


# ── Fake Claude API ──

_delay: tuple[float, float] = (0.05, 0.15)
_fail_rate: float = 0.0
_api_sem: asyncio.Semaphore | None = None

@rate_limited(n=5, memo=True)  # no memo — can fail, retried by llm wrapper
async def fake_api_call(prompt: str) -> str:
    """Simulates a API call. Fails ~20% of the time, rate limited."""
    # Rate limit
    trace("calling LLM API", prompt_len=len(prompt), status="running")
    await asyncio.sleep(random.uniform(*_delay))

    # Random failures
    if random.random() < _fail_rate:
        trace("API error", status="error")
        raise ConnectionError("Claude API: 529 Overloaded")

    # Deterministic-ish response based on prompt hash
    h = hashlib.md5(prompt.encode()).hexdigest()[:8]
    return _generate_response(prompt, h)

def _generate_response(prompt: str, h: str) -> str:
    p = prompt.lower()
    if "research" in p:
        return (
            f"Report [{h}]: The subject shows interesting characteristics including "
            f"unique habitat preferences in temperate zones, omnivorous dietary patterns "
            f"with seasonal variation, and complex social hierarchies. Population estimates "
            f"suggest {random.randint(1000, 50000)} individuals in the wild."
        )
    if "validate" in p:
        # Fail ~60% on first attempt — forces retry loop to exercise
        if int(h, 16) % 5 < 3:
            return '{"success": false, "feedback": "Report lacks specific data points. Add quantitative measurements and cite sources."}'
        return '{"success": true, "feedback": null}'
    if "refine" in p:
        return (
            f"Refined report [{h}]: Detailed analysis reveals measurable characteristics. "
            f"Population density: ~{random.randint(50, 5000)}/km². "
            f"Average lifespan: {random.randint(5, 40)} years. "
            f"Diet: {random.randint(30, 70)}% vegetation, remainder protein. "
            f"Conservation status: {random.choice(['Vulnerable', 'Endangered', 'Least Concern', 'Near Threatened'])}."
        )
    return f"Response [{h}]: {prompt[:80]}..."


# ── LLM wrapper with retry ──


@step(memo=True)  # cache successful LLM calls — the expensive leaf
async def llm(prompt: str) -> str:
    """LLM call with automatic retry on API failures."""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            if attempt > 0:
                trace("retrying API call", attempt=attempt + 1)
                await asyncio.sleep(0.5 * attempt)  # backoff
            return await fake_api_call(prompt)
        except ConnectionError as e:
            last_error = e
            trace("API failed, will retry", error=str(e), attempt=attempt + 1)
    raise last_error or ConnectionError("max retries exceeded")


# ── Pipeline steps ──


@step
async def research(subject: str, spec: str) -> str:
    trace("researching", subject=subject)
    return await llm(f"Research {subject} according to: {spec}")


@step
async def validate(spec: str, report: str) -> dict[str, object]:
    import json
    raw = await llm(
        f"Validate this report against spec.\nSpec: {spec}\n"
        f"Report: {report}\nOutput JSON: {{success: bool, feedback: str}}"
    )
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        return {"success": True, "feedback": None}


@step
async def refine(subject: str, draft: str, feedback: str) -> str:
    return await llm(f"Refine report on {subject}.\nDraft: {draft}\nFeedback: {feedback}")


@step(memo=True)
async def research_validated(subject: str, spec: str) -> str:
    """Research with validation loop — retries until validated or max attempts."""
    draft = await research(subject, spec)
    for i in range(3):
        trace("validating", progress=(i + 1, 3), subject=subject)
        result = await validate(spec, draft)
        if result.get("success"):
            trace("validated", subject=subject, attempts=i + 1)
            return draft
        feedback = str(result.get("feedback", "needs improvement"))
        trace("retrying", edge=True, attempt=i + 1, feedback=feedback)
        draft = await refine(subject, draft, feedback)
    trace("max retries reached", subject=subject)
    return draft


# ── Entry points ──

ANIMALS_SMALL = ["Red Fox", "Giant Octopus", "Monarch Butterfly", "Snow Leopard"]

ANIMALS_LARGE = [
    "Red Fox", "Giant Octopus", "Monarch Butterfly", "Snow Leopard",
    "Blue Whale", "Honey Badger", "Komodo Dragon", "Arctic Tern",
    "Mantis Shrimp", "Pangolin", "Axolotl", "Peregrine Falcon",
    "Leafy Sea Dragon", "Capybara", "Harpy Eagle", "Narwhal",
    "Tasmanian Devil", "Okapi", "Cassowary", "Dumbo Octopus",
]

SPEC = "Comprehensive report covering habitat, diet, behavior, and conservation status."


@step
async def pipeline(subjects: list[str] | None = None) -> dict[str, str]:
    """Research pipeline: fan-out across subjects."""
    animals = subjects or ANIMALS_SMALL
    trace("starting pipeline", subject_count=len(animals))

    handles = {s: research_validated(s, SPEC) for s in animals}

    results: dict[str, str] = {}
    for subject, handle in handles.items():
        results[subject] = await handle
    
    done = ", ".join(subjects or results.keys())
    trace(f"completed ({done})")

    trace("pipeline complete")
    return results


@step
async def pipeline_slow() -> dict[str, str]:
    """Full pipeline: 20 animals, 1-4s delays, 20% failure rate, rate limited to 5 concurrent."""
    global _delay, _fail_rate, _api_sem  # noqa: PLW0603
    _delay = (1.0, 4.0)
    _fail_rate = 0.2
    _api_sem = asyncio.Semaphore(5)
    return await pipeline(ANIMALS_LARGE)

slow = pipeline_slow

main = pipeline


if __name__ == "__main__":
    print("Running research pipeline...")
    print("Store: .cairn/\n")
    results = run(pipeline, store_path=".cairn")
    print(f"\nCompleted {len(results)} reports:\n")
    for subject, report in results.items():
        print(f"  {subject}: {report[:80]}...")
    print(f"\nExplore: cairn list && cairn show")
