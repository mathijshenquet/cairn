# Cairn: Motivation

## The problem

You're building a due diligence pipeline for 200 companies. Each company needs an AI-generated analysis, validated against a rubric, refined if it doesn't pass, and reviewed by a human before it's final. You get the first 10 working after a day of prompt iteration. Then the API rate-limits you at company 47. You restart, and it reruns all 47 from scratch — $30 in API calls, gone. You tweak the validation prompt. Everything reruns again. A reviewer flags an issue with the rubric at company 150. You want to rerun just the validation step for everything already processed, without re-generating the analyses.

The simplest version of this is a bash script:

```bash
for company in $(cat companies.txt); do
  claude --bare -p "analyze $company" > "$company.md"
done
```

This works but gives you nothing: no caching, no observability, no human-in-the-loop, no retry, no concurrency. When the API fails at company 47, you rerun the whole thing.

## What exists today

**Heavy frameworks** (LangGraph, CrewAI, AutoGen) make you learn a new abstraction layer. You define graphs, nodes, edges, agents with roles. The framework owns the execution model. Composability is limited to what the framework anticipated — a researcher+validator loop is not a reusable building block, it's a specific graph you wire up each time.

**The rigid graph problem.** These frameworks use a TensorFlow-era model: you declare a static graph, then execute it. This lacks expressive power. A researcher+validator pair should be a higher-order composable component — a function that takes a task and a spec and returns a validated result. You can't do this cleanly when your "graph" is a flat list of nodes and edges.

**The master agent anti-pattern.** Some frameworks solve orchestration by putting an agent in charge. The agent decides what to call next. This is the opposite of what you want — the script is supposed to be the rails keeping everything on track, not another source of unpredictability.

## The analogy

In ML, TensorFlow had you declare a computation graph, then execute it in a session. It was explicit but rigid. PyTorch came along and said: just write Python. The computation graph is defined by running it. You get autograd, GPU acceleration, and debugging for free — because the framework instruments ordinary Python rather than replacing it.

Cairn is PyTorch for compute graph orchestration. You write async Python functions. A decorator instruments them to get tracing, caching, and observability. The computation graph emerges from the actual execution — no graph DSL, no special node types, no edges to wire up.

## What Cairn is

A small Python library with one core idea: a decorator that turns an async function into a tracked, cached node in a computation graph.

From this you get:

- **Caching** — deterministic replay based on (function identity + version + inputs). Change nothing, rerun instantly. Change one function, only its downstream re-executes.
- **Observability** — a live trace of the execution. What's running, what's pending, what failed, how long things took. Consumed by any frontend.
- **Composability** — higher-order patterns (retry loops, validation loops, fan-out/fan-in) are just functions that call other functions. No special graph primitives.
- **Human-in-the-loop** — a human review step is just another async function that blocks until input arrives. The framework doesn't care how input arrives (web UI, TUI, Telegram, Slack).
- **Replay** — cached traces include timing. You can replay a full pipeline with simulated timing for demos, testing, or sharing workflows without API keys.
- **Restartability** — rerun a failed pipeline. Completed steps are cached, execution resumes from where it left off.

## What Cairn is not

- Not an agent framework. There are no agents, roles, or tool definitions. You write the logic, Cairn tracks and caches it.
- Not AI-specific. The same primitives work for scrapers, ETL pipelines, data processing — anything expressible as async functions with deterministic outputs.
- Not a workflow engine. No DAGs to deploy, no scheduler, no cluster. It's a library you import.

## Key user stories

### 1. AI research pipeline with iteration

A researcher iterates on a prompt for a single subject with human feedback, then fans out to N subjects once the prompt is good. Each subject goes through a research+validate loop. Everything is cached — rerunning after a failure skips completed work.

### 2. Composable agent patterns

A research+validator loop is a function, not a graph. You can parameterize it, nest it, reuse it. Rate limiting, retry, replay — all higher-order wrappers built from the same primitives. A `replayable(fn)` wrapper is 10 lines, not a framework feature.

### 3. Human-in-the-loop at configurable points

During iteration, a human reviews each output. During bulk execution, everything runs autonomously. The switch is just whether you include a human review step in the pipeline — not a framework mode or configuration flag.

### 4. Web scraper with caching

Scrape a site, parse pages, extract data. Pages already fetched are cached. Add a new extraction step, rerun — only the new step executes. No AI involved, same framework.

### 5. Replay, demos, and privacy

Run a pipeline once with real API calls. Replay it later with simulated timing for a demo. The audience sees the full execution progression — tasks starting, progressing, completing — without any API calls or human interaction. Swap in `replayable(fn)` wrappers, everything else unchanged.

Replay also serves a privacy purpose: a `replayable` task is indistinguishable from a live one. No `cached: true` flag in the trace, no evidence that results came from a cache. You can share a pipeline run without leaking your caching infrastructure or revealing which calls were precomputed.

### 6. Monitoring a live pipeline

A web UI (or TUI, or Telegram bot) shows the live execution trace. Tasks starting, progress updates, fan-out visualized, errors highlighted. The same trace log drives all frontends. The framework emits events, the frontend renders them.

### 7. Tweaking and re-execution

Change one function's implementation. Rerun. Only that function and its downstream dependents re-execute — everything else is cached. The version hash (derived from the function body) automatically invalidates the right cache entries. No manual cache busting.
