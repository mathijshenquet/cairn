# Cairn: Patterns and Evaluation

## Evaluation rubric

We evaluate each pattern across six dimensions:

| Dimension | What it measures |
|-----------|-----------------|
| **Clarity** | Can you read the code and understand what it does? Is the intent obvious? |
| **Natural fit** | Does the pattern feel like normal Python, or does the framework fight you? |
| **Composability** | Can this pattern be abstracted, parameterized, and reused as a building block? |
| **Observability** | Can you see what's happening at runtime? Progress, status, dependencies? |
| **Cacheability** | Does re-execution skip completed work? Is cache invalidation correct? |
| **Conciseness** | How much code does it take? What's the signal-to-noise ratio? |

Each scored 1–5 per framework. We'll fill in the matrix after showing the patterns.

---

## Pattern 1: Fan-out / fan-in

Process N items concurrently, collect results.

### Cairn

```python
@_step
async def process(item: str) -> str:
    return await claude(f"analyze {item}")

@_step
async def pipeline(items: list[str]) -> list[str]:
    handles = [process(item) for item in items]
    return [await h for h in handles]
```

Concurrency is implicit — calling a `_step` returns a `Handle` immediately, the work runs in the background. No special `map` API, no task runner configuration. It's just Python list comprehensions.

### Prefect

```python
@task
def process(item: str) -> str:
    return claude(f"analyze {item}")

@flow(task_runner=ConcurrentTaskRunner())
def pipeline(items: list[str]) -> list[str]:
    futures = process.map(items)
    return [f.result() for f in futures]
```

Requires choosing a task runner upfront. `.map()` is a Prefect-specific method, not Python. `.result()` blocks.

### LangGraph

```python
# Fan-out requires Send() API and careful state schema design
class State(TypedDict):
    items: list[str]
    results: list[str]

def scatter(state):
    return [Send("process", {"item": i}) for i in state["items"]]

def process(state):
    return {"results": [claude(f"analyze {state['item']}")]}

graph = StateGraph(State)
graph.add_node("process", process)
graph.add_conditional_edges("__start__", scatter)
# ... more wiring
```

Dynamic fan-out requires the `Send()` API and conditional edges. The state schema must be designed upfront to accommodate scattered results. Verbose.

### Flyte

```python
@task(cache=True, cache_version="1")
def process(item: str) -> str:
    return claude(f"analyze {item}")

@workflow
def pipeline(items: list[str]) -> list[str]:
    return map_task(process)(item=items)
```

Clean. `map_task` is purpose-built for this. Caching is first-class. But requires Kubernetes.

### Temporal

```python
@activity.defn
async def process(item: str) -> str:
    return await claude(f"analyze {item}")

@workflow.defn
class Pipeline:
    @workflow.run
    async def run(self, items: list[str]) -> list[str]:
        return await asyncio.gather(*[
            workflow.execute_activity(process, item,
                start_to_close_timeout=timedelta(minutes=5))
            for item in items
        ])
```

`asyncio.gather` works naturally but wrapped in `execute_activity` ceremony. Requires Temporal server.

---

## Pattern 2: Retry with validation loop

Generate, validate, refine in a loop.

### Cairn

```python
@_step
async def research(subject: str, spec: str) -> str:
    return await claude(f"research {subject} per {spec}")

@_step
async def validate(spec: str, report: str) -> dict:
    return json.loads(await claude(f"validate against {spec}: {report}"))

@_step
async def refine(draft: str, feedback: str) -> str:
    return await claude(f"improve: {draft}\nfeedback: {feedback}")

@_step
async def research_validated(subject: str, spec: str) -> str:
    draft = await research(subject, spec)
    for i in range(3):
        trace("validating", progress=(i + 1, 3))
        result = await validate(spec, draft)
        if result["success"]:
            return draft
        trace("retrying", edge=True, feedback=result["feedback"])
        draft = await refine(draft, result["feedback"])
    return draft
```

This is just a function with a loop. Each step is cached independently — on re-run, `research` is a cache hit, validation reruns from the refined draft. The loop is a Python `for`, not a graph edge. You can extract `research_validated` as a reusable higher-order pattern trivially.

### LangGraph

```python
class State(TypedDict):
    subject: str
    spec: str
    draft: str
    feedback: str
    attempt: int

def research(state):
    return {"draft": claude(f"research {state['subject']}")}

def validate(state):
    result = json.loads(claude(f"validate: {state['draft']}"))
    return {"feedback": result.get("feedback", ""), "attempt": state["attempt"] + 1}

def should_retry(state):
    if state["attempt"] >= 3:
        return "done"
    if "feedback" in state and state["feedback"]:
        return "refine"
    return "done"

def refine(state):
    return {"draft": claude(f"improve: {state['draft']}\n{state['feedback']}")}

graph = StateGraph(State)
graph.add_node("research", research)
graph.add_node("validate", validate)
graph.add_node("refine", refine)
graph.add_edge("research", "validate")
graph.add_conditional_edges("validate", should_retry, {"refine": "refine", "done": END})
graph.add_edge("refine", "validate")
graph.set_entry_point("research")
```

The loop is encoded as a conditional edge cycle. State must be threaded through a TypedDict. The validation logic is split across `validate` and `should_retry`. Composing this with other graphs requires state schema alignment.

### Prefect

```python
@task(retries=0)
def research(subject, spec):
    return claude(f"research {subject} per {spec}")

@task
def validate(spec, report):
    return json.loads(claude(f"validate: {report}"))

@task
def refine(draft, feedback):
    return claude(f"improve: {draft}\n{feedback}")

@flow
def research_validated(subject, spec):
    draft = research(subject, spec)
    for i in range(3):
        result = validate(spec, draft)
        if result["success"]:
            return draft
        draft = refine(draft, result["feedback"])
    return draft
```

Similar to Cairn. Prefect's decorator approach is close. But `.submit()` vs direct call semantics can be confusing. No per-step caching by default — caching requires `cache_key_fn` on each task.

---

## Pattern 3: Human-in-the-loop

Iterative refinement with human feedback.

### Cairn

```python
@_step(memo=False)
async def human_review(question: str) -> str:
    prev = cached_output()
    return await harness.ask(question, prefill=prev)

@_step
async def iterate_spec(initial_spec: str, sample_subject: str) -> str:
    spec = initial_spec
    for i in range(5):
        report = await research_validated(sample_subject, spec)
        feedback = await human_review(
            f"Report on {sample_subject}:\n\n{report}\n\nRevise spec or type 'ok':"
        )
        if feedback.strip().lower() == "ok":
            return spec
        spec = feedback
        trace("spec revised", edge=True, iteration=i + 1)
    return spec
```

Human interaction is just another step. The loop is Python. `cached_output()` prefills the answer on re-run so the human can accept the previous answer or change it. Nothing special in the framework.

### LangGraph

```python
# Must use interrupt_before/interrupt_after
graph.add_node("human_review", human_review)
graph.compile(checkpointer=MemorySaver(), interrupt_before=["human_review"])

# To resume after human input:
# graph.invoke(Command(resume=human_input), config={"configurable": {"thread_id": "1"}})
```

Human-in-the-loop is a first-class feature but tightly coupled to the graph model. The interrupt/resume pattern requires managing thread IDs and understanding the checkpoint/replay mechanism.

### Temporal

```python
@workflow.defn
class ReviewWorkflow:
    def __init__(self):
        self._human_input = None

    @workflow.signal
    async def submit_review(self, input: str):
        self._human_input = input

    @workflow.run
    async def run(self, spec: str):
        for i in range(5):
            report = await workflow.execute_activity(research, spec, ...)
            await workflow.wait_condition(lambda: self._human_input is not None)
            feedback = self._human_input
            self._human_input = None
            if feedback == "ok":
                return spec
            spec = feedback
```

Temporal's signal pattern is powerful and durable (survives process restarts) but requires class-based workflows and understanding the signal/query model.

---

## Pattern 4: Replayable execution

Re-run a pipeline with simulated timing, no real API calls.

### Cairn

```python
# Swap wrappers, everything else unchanged
claude = replayable(claude_impl)

# Run — replays from cache with original timing
await pipeline()
```

`replayable` is a 10-line user-space wrapper (see design doc). The trace is indistinguishable from a live run. No framework mode, no flag. You just swap the implementation.

### Others

No direct equivalent in any competitor. Temporal has replay for determinism testing, but it's for verifying workflow correctness, not for demos/privacy. LangGraph checkpoints can be replayed but without timing simulation. Prefect/Dagster/Flyte have no concept of this.

---

## Pattern 5: Rate limiting

Limit concurrent API calls.

### Cairn

```python
@rate_limited(5)
async def claude(prompt: str) -> str:
    return await api_call(prompt)
```

Where `rate_limited` is a ~15-line wrapper using a semaphore + trace annotations (see design doc). The UI shows tasks as "pending" (waiting for slot) vs "running" (executing).

### Prefect

```python
# Use task concurrency limits (server-side)
@task(tags=["claude"])
def claude(prompt: str) -> str:
    return api_call(prompt)

# Configure via CLI or API:
# prefect concurrency-limit create claude --limit 5
```

Server-side configuration, not in the code. Requires Prefect server.

### Temporal

```python
# Activity-level rate limiting configured on the worker
worker = Worker(
    client, task_queue="main",
    activities=[claude],
    max_concurrent_activities=5
)
```

Worker-level configuration. Clean but external to the workflow code.

### Others

LangGraph, CrewAI, Dagster, Flyte: no built-in rate limiting. Use external libraries (tenacity, aiolimiter) within task bodies.

---

## Pattern 6: Composable higher-order patterns

A `validated()` function that works with any generate/validate/refine triple.

### Cairn

```python
async def validated(generate_fn, validate_fn, refine_fn, *args, max_retries=3, **kwargs):
    draft = await generate_fn(*args, **kwargs)
    for i in range(max_retries):
        result = await validate_fn(draft)
        if result["success"]:
            return draft
        trace("retrying", edge=True, attempt=i + 1)
        draft = await refine_fn(draft, result["feedback"])
    return draft

# Usage — different domains, same pattern:
report = await validated(research, validate_report, refine_report, subject="cat", spec=spec)
code = await validated(generate_code, run_tests, fix_code, task="sort function")
essay = await validated(write_essay, check_grammar, revise, topic="climate change")
```

It's just a function. It takes functions. It returns a result. No graph algebra, no special composition API.

### LangGraph

```python
# You'd need to create a reusable subgraph factory:
def make_validated_graph(research_fn, validate_fn, refine_fn):
    class State(TypedDict):
        draft: str
        feedback: str
        attempt: int
    graph = StateGraph(State)
    graph.add_node("generate", research_fn)
    graph.add_node("validate", validate_fn)
    graph.add_node("refine", refine_fn)
    graph.add_edge("generate", "validate")
    graph.add_conditional_edges("validate", should_retry, ...)
    graph.add_edge("refine", "validate")
    return graph.compile()
```

Possible but verbose. The state schema must be shared, which couples the components. The graph factory returns a compiled graph, not a simple callable — composing it with other graphs requires more wiring.

### Prefect

```python
# Similar to Cairn — flows call tasks, so you can write a helper function.
# But .submit() vs direct call confusion makes it trickier.
@flow
def validated(generate_fn, validate_fn, refine_fn, *args, **kwargs):
    draft = generate_fn(*args, **kwargs)
    for i in range(3):
        result = validate_fn(draft)
        if result["success"]:
            return draft
        draft = refine_fn(draft, result["feedback"])
    return draft
```

Close. But `validated` itself must be a `@flow` (not just a function) to get tracking. And the generate/validate/refine functions must be `@task`-decorated. Layering flows calling flows calling tasks can get confusing.

---

## Pattern 7: RLHF-style feedback loop

Generate N candidates, have human rank them, use ranking to improve.

### Cairn

```python
@_step
async def generate_candidates(prompt: str, n: int) -> list[str]:
    handles = [claude(f"{prompt}\n(variation {i})") for i in range(n)]
    return [await h for h in handles]

@_step(memo=False)
async def human_rank(candidates: list[str]) -> list[int]:
    prev = cached_output()
    return await harness.rank(candidates, prefill=prev)

@_step
async def improve(prompt: str, ranked: list[str]) -> str:
    best = ranked[0]
    return await claude(f"improve on this:\n{best}\noriginal prompt: {prompt}")

@_step
async def rlhf_loop(prompt: str, rounds: int = 3) -> str:
    for i in range(rounds):
        trace("round", progress=(i + 1, rounds))
        candidates = await generate_candidates(prompt, n=5)
        ranking = await human_rank(candidates)
        ordered = [candidates[i] for i in ranking]
        best = await improve(prompt, ordered)
        prompt = f"Continue improving: {best}"
    return best
```

The RLHF loop is a plain for-loop. Fan-out for candidates is implicit. Human ranking is a step with prefill. Each round is independently cached.

### LangGraph

Would require a stateful graph with human interrupt at the ranking node, careful state management of the candidate list and ranking, and conditional edges for the loop. Easily 50+ lines of graph construction.

### CrewAI

```python
# CrewAI doesn't really support this — you'd need:
critic = Agent(role="Critic", ...)
tasks = [Task(description=f"Generate variation {i}", agent=writer) for i in range(5)]
ranking_task = Task(description="Rank the outputs", agent=critic, human_input=True)
```

The human ranking is just a flag. No prefill, no structured ranking UI. The loop requires building a custom process or nesting crews.

---

## Evaluation matrix

| Pattern | Dim. | Cairn | Prefect | LangGraph | CrewAI | Temporal | Flyte |
|---------|------|------|---------|-----------|--------|----------|-------|
| **Fan-out** | Clarity | 5 | 4 | 3 | 2 | 3 | 4 |
| | Natural fit | 5 | 3 | 2 | 1 | 3 | 3 |
| | Composability | 5 | 4 | 2 | 1 | 3 | 4 |
| | Observability | 3 | 4 | 3 | 2 | 5 | 4 |
| | Cacheability | 5 | 3 | 2 | 2 | 4 | 5 |
| | Conciseness | 5 | 4 | 2 | 2 | 3 | 4 |
| **Retry loop** | Clarity | 5 | 4 | 3 | 2 | 3 | 3 |
| | Natural fit | 5 | 4 | 2 | 2 | 3 | 3 |
| | Composability | 5 | 4 | 2 | 1 | 3 | 2 |
| | Observability | 3 | 3 | 3 | 2 | 4 | 3 |
| | Cacheability | 5 | 2 | 3 | 2 | 4 | 3 |
| | Conciseness | 5 | 4 | 2 | 3 | 3 | 3 |
| **Human-in-loop** | Clarity | 5 | 3 | 3 | 4 | 3 | 2 |
| | Natural fit | 5 | 3 | 3 | 3 | 3 | 2 |
| | Composability | 5 | 3 | 2 | 1 | 3 | 2 |
| | Observability | 3 | 3 | 3 | 2 | 4 | 2 |
| | Cacheability | 5 | 1 | 3 | 1 | 4 | 1 |
| | Conciseness | 5 | 3 | 2 | 4 | 2 | 2 |
| **Replay** | All dims | 5 | 1 | 2 | 1 | 3 | 1 |
| **Rate limit** | Clarity | 5 | 3 | 1 | 1 | 3 | 1 |
| | Natural fit | 5 | 2 | 1 | 1 | 3 | 1 |
| **Composability** | All dims | 5 | 4 | 2 | 1 | 3 | 3 |
| **RLHF loop** | All dims | 5 | 3 | 2 | 2 | 3 | 2 |

### Caveats and scoring honesty

- **Cairn does not yet exist** — scores reflect design intent, not proven implementation. Every Cairn score should be read as "if implemented as designed."
- **Observability scores for Cairn are low** (2-3) because we have a JSONL format, not a UI. Competitors like Temporal, Prefect, and Dagster have mature dashboards, alerting, and operational tooling. Cairn's observability *design* is sound but unproven.
- **Pattern selection is biased toward Cairn's strengths**: composability, caching, higher-order patterns. We did not evaluate patterns where competitors excel: streaming token output (LangGraph), distributed execution (Flyte/Dagster), durable long-running workflows (Temporal), asset lineage (Dagster).
- **LangGraph's newer functional API is cleaner** than the verbose `StateGraph` construction shown here. Scores may be 1 point too low on clarity.
- **Prefect is closer to Cairn than it looks.** A `@flow` with `@task` calls in a for-loop achieves a similar pattern. The main gaps are per-step caching granularity and the `.submit()` vs direct-call ergonomics.
- Temporal's low scores on conciseness/natural-fit are the cost of its strongest-in-class durability guarantees. If you need to survive process crashes, Temporal is the right choice.
- CrewAI is designed for a different use case (quick multi-agent prototyping) and shouldn't be judged purely on programmatic control.
- Flyte has excellent caching but requires Kubernetes, which puts it in a different deployment category.

### Where Cairn is weakest

- **Production observability**: we have a log format, not tooling. Competitors have years of UI/alerting investment. On the roadmap for v2.
- **Ecosystem**: no existing integrations, UI, or community. Competitors have years of investment. Chicken-and-egg.
- **Streaming**: no first-class support for streaming LLM token output. LangGraph handles this natively. Out of scope for Cairn.

### Not real weaknesses (on inspection)

- **Durability**: the output store gives crash recovery for completed steps. In-flight work is lost on crash, but this is an engineering detail (atomic JSONL writes, fsync) not an architectural gap. Temporal is still ahead here but the gap is narrower than it looks.
- **Deployment**: `Handle[T]` already abstracts over execution location. Swapping `asyncio.create_task` for a task queue backend (Celery, NATS, Redis) enables distribution without changing user code. The output store is similarly pluggable. Upgrade path is clear.

### Where Cairn wins

- **Composability**: higher-order patterns are just functions. Prefect is close; nothing else comes close.
- **Caching**: per-step, content-addressed, automatic invalidation on code change. Flyte is the only competitor with comparable caching.
- **Replay**: unique feature. No competitor offers privacy-preserving replay with timing simulation.
- **Learning curve**: if you know async Python, you know Cairn. No graphs, no state schemas, no servers.
- **Conciseness**: consistently fewest lines of code for equivalent functionality.
