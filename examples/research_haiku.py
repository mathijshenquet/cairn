"""Live Claude research pipeline.

Invokes the `claude` CLI (`-p` + `--system-prompt`) with the haiku model to
do real web research on a handful of AI companies. The current ISO week is
threaded through every step, so it becomes part of the cache key — re-runs
within the same week reuse cached results; a new week forces fresh fetches.

Demonstrates a small back-and-forth loop (angle searches → critique →
refine) per company, followed by a cross-company synthesis step.

Run:
    cairn examples/research_haiku.py
"""

from __future__ import annotations

import asyncio
from datetime import date

from cairn import run, step, trace
from cairn.patterns import rate_limited


# Why no --bare: it forces auth to ANTHROPIC_API_KEY (OAuth/keychain disabled),
# which means a Claude subscription (CLAUDE_CODE_OAUTH_TOKEN) doesn't work.
# Without --bare, CLAUDE.md and auto-memory leak into the default system prompt,
# so we pass --system-prompt to replace it entirely with a focused research one.
SYSTEM_PROMPT = (
    "You are a concise research assistant. Use WebSearch and WebFetch when "
    "current information is needed. Answer directly, no preamble, no "
    "meta-commentary. Respect terseness instructions in the user prompt."
)


COMPANIES = [
    "Anthropic",
    "OpenAI",
    "Google DeepMind",
    "Mistral AI",
    "Cohere",
]

ANGLES = [
    "product launches and model releases",
    "funding, partnerships, or leadership changes",
    "research output or benchmark results",
]

MODEL = "haiku"
SEARCH_TOOLS = "WebSearch,WebFetch"
# Belt-and-suspenders: `--tools` is already a whitelist, but deny writes explicitly.
DENY_TOOLS = "Write,Edit,NotebookEdit,Bash,MultiEdit"


def current_week() -> str:
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


async def _claude(prompt: str, tools: str = "") -> str:
    """Invoke `claude -p` with a minimal system prompt; return stdout."""
    args = [
        "claude",
        "-p",
        "--model", MODEL,
        "--system-prompt", SYSTEM_PROMPT,
        "--permission-mode", "bypassPermissions",
        "--tools", tools,
        "--disallowed-tools", DENY_TOOLS,
        "--",
        prompt,
    ]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = (stderr.decode().strip() or stdout.decode().strip() or "(no output)")[:500]
        raise RuntimeError(f"claude exited {proc.returncode}: {msg}")
    return stdout.decode().strip()


@rate_limited(n=3, memo=True)
async def angle_search(company: str, angle: str, week: str) -> str:
    """Web-search a single angle on a company. `week` is cache-bust."""
    trace("searching", company=company, angle=angle, week=week)
    prompt = (
        f"Search the web for news from the past 7 days about {company}, "
        f"focused on: {angle}. Current ISO week: {week}. "
        f"Return the 2-3 most notable items: headline, date, one-sentence "
        f"summary, source URL. Plain text, bulleted. No preamble."
    )
    return await _claude(prompt, tools=SEARCH_TOOLS)


@step(memo=True)
async def critique(company: str, findings: dict[str, str]) -> str:
    """Critique the raw findings: what's missing, weak, or contradictory."""
    trace("critiquing", company=company)
    joined = "\n\n".join(f"## {a}\n{f}" for a, f in findings.items())
    prompt = (
        f"You are a skeptical analyst reviewing findings on {company}. "
        f"Identify gaps, weak claims, or things that warrant more context. "
        f"Be terse — 3 bullets max.\n\nFindings:\n{joined}"
    )
    return await _claude(prompt)


@step(memo=True)
async def refine(company: str, findings: dict[str, str], critique_text: str) -> str:
    """Produce a polished company brief incorporating the critique."""
    trace("refining", company=company)
    joined = "\n\n".join(f"## {a}\n{f}" for a, f in findings.items())
    prompt = (
        f"Write a tight one-paragraph brief on {company} based on the "
        f"findings below. Address the critique by softening weak claims "
        f"and noting gaps explicitly. No preamble.\n\n"
        f"Findings:\n{joined}\n\nCritique:\n{critique_text}"
    )
    return await _claude(prompt)


@step
async def research_company(company: str, week: str) -> dict[str, str]:
    """Fan out across angles, critique, then refine into a brief."""
    trace("researching", company=company)

    handles = {a: angle_search(company, a, week) for a in ANGLES}
    findings: dict[str, str] = {}
    for angle, h in handles.items():
        findings[angle] = await h

    crit = await critique(company, findings)
    brief = await refine(company, findings, crit)
    return {"brief": brief, "critique": crit}


@step(memo=True)
async def synthesize(briefs: dict[str, str], week: str) -> str:
    """Cross-company synthesis: what's the landscape looking like this week."""
    trace("synthesizing", company_count=len(briefs), week=week)
    joined = "\n\n".join(f"## {c}\n{b}" for c, b in briefs.items())
    prompt = (
        f"You are a strategist. Given these per-company briefs for ISO week "
        f"{week}, write a 4-sentence synthesis of the AI landscape this week: "
        f"common themes, who stood out, what's next. No preamble.\n\n{joined}"
    )
    return await _claude(prompt)


@step
async def pipeline() -> dict[str, object]:
    week = current_week()
    trace("pipeline start", week=week, companies=len(COMPANIES))

    handles = {c: research_company(c, week) for c in COMPANIES}
    per_company: dict[str, dict[str, str]] = {}
    for company, h in handles.items():
        per_company[company] = await h

    briefs = {c: data["brief"] for c, data in per_company.items()}
    landscape = await synthesize(briefs, week)

    trace("pipeline complete")
    return {"week": week, "companies": per_company, "landscape": landscape}


main = pipeline


if __name__ == "__main__":
    print(f"Researching {len(COMPANIES)} AI companies via Claude Haiku...")
    result = run(pipeline, store_path=".cairn")
    week = result["week"]
    landscape = result["landscape"]
    companies: dict[str, dict[str, str]] = result["companies"]  # type: ignore[assignment]
    print(f"\n=== Landscape ({week}) ===\n{landscape}\n")
    for company, data in companies.items():
        print(f"--- {company} ---")
        print(data["brief"])
        print()
