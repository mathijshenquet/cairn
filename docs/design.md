# Cairn: Design

## Overview

Cairn has five core primitives:

| Primitive | Role |
|-----------|------|
| `step` decorator | Turns an async function into a tracked, cached node in the computation graph |
| `Handle[T]` | Awaitable reference to a running step's result. Enables concurrency and dependency tracking |
| `trace()` | Formless annotation — emits an atomic event into the trace log |
| `cached_output()` | Access previous cached result from within a step body |
| `cached_tracing()` | Access previous trace events with timestamps — enables replay |

One entry point: `run()` (programmatic) or `cairn script.py` (CLI, streamlit-style).

Everything else — memoization, replay, rate limiting, retry, human-in-the-loop — is built from these primitives in user space or thin library wrappers.

---

## 1. The `@step` decorator

### Signature

```python
def step(
    fn: Callable[P, Awaitable[R]] = None,
    *,
    memo: bool = False,
    identity: str | Identity | Callable[[Any], str | Identity] | None = None,
    version: str | Version | Callable[[Any], str | Version] | None = None,
) -> Callable[P, Handle[R]]:
    ...
```

### Why `memo=False` by default

`memo=True` means "on cache hit, skip execution entirely and return the stored result." That's the right choice for expensive leaves (LLM calls, heavy computation) but the wrong default for orchestration steps — a memoized step that hits cache doesn't re-execute, which means its child steps never re-spawn, and the computation graph for a rerun looks empty.

The asymmetry: an un-memoed step can reconstruct memoed behavior by calling `cached_output()` early and returning. The inverse isn't possible — a memoed step can never choose to re-run its children for observability. So the less-opinionated default (`memo=False`) is strictly more expressive.

### Overloads for usage as decorator

```python
# bare decorator — always runs, identity/version auto-derived
@step
async def foo(x: int) -> int: ...

# decorator with options — opt into memoization for expensive leaves
@step(memo=True)
async def bar(x: int) -> int: ...

# functional form (used by higher-order wrappers)
wrapped = step(fn, memo=True, identity=Identity.from_function(original))
```

### What the decorator does

When a decorated function is called:

1. **Capture parent** — read `_current_span` from `contextvars` to establish the parent-child relationship.
2. **Create span** — allocate a `TaskSpan` with a unique ID, parent reference, identity, version, and raw arguments.
3. **Schedule execution** — create an `asyncio.Task` running the step body within a task group.
4. **Emit spawn event** — write to the trace log.
5. **Return `Handle[T]`** — immediately, without blocking.

The scheduled execution then:

1. **Set context** — push this span onto `_current_span` so child steps and `trace()` calls know their parent.
2. **Resolve Handle arguments** — any argument that is a `Handle[T]` is awaited, yielding `T`. This wires up the dependency graph. Join events are emitted for each resolved Handle.
3. **Compute cache key** — `hash(identity, version, resolved_args)`.
4. **Check cache** — look up the key in the output store.
5. **If cached and `memo=True`** — emit an `end` event with `cached: true`, return the stored result. The function body is never called.
6. **If cached and `memo=False`** — store the cached result/traces in the span context (accessible via `cached_output()` and `cached_tracing()`). Then call the function body.
7. **If not cached** — call the function body.
8. **Store result** — write (result, collected traces, duration) to the output store.
9. **Emit end event** — write to the trace log.
10. **Wait for task group** — the span doesn't close until all child steps (spawned within this step) have completed. This enforces structured concurrency.

### Identity and version

**Identity** answers: "what function is this?" It's the stable name across code changes.

- Default: `f"{module}:{qualname}"` (e.g., `pipeline:research_loop`)
- Override: any string or `Identity` object

**Version** answers: "which implementation of this function?" It changes when the code changes.

- Default: hash of `inspect.getsource(fn)` (v1). Recursive dependency hashing in v2.
- Override: any string or `Version` object

Identity and version are both overridable via the decorator or via higher-order wrappers. This is essential for patterns like `replayable(fn)` which needs to share the original function's identity and version.

Display:
- `identity.short()` — human-readable, e.g., first 8 hex digits
- `identity.long()` — full hash or qualified name
- Same for `version.short()` / `version.long()`

### Arguments and the cache key

The cache key is `sha256(canonical(identity, version, resolved_args))`. Arguments must be reducible to a canonical hashable form.

Built-in support:
- Primitives: `str`, `int`, `float`, `bool`, `None`
- Collections: `list`, `tuple`, `dict` (sorted keys), `frozenset`
- Nested combinations of the above

For everything else, a `hash_funcs` registry (Streamlit-style):

```python
configure(hash_funcs={
    Path: lambda p: (str(p), p.stat().st_mtime) if p.exists() else str(p),
    DatabaseConnection: lambda db: db.url,
})
```

Resolution uses MRO — registering for `BaseModel` covers all Pydantic subclasses. The hash function returns something the framework re-resolves recursively, so `Path` returning `(str, float)` just works.

### Return type and serialization

Results must be serializable for caching. Built-in support:

- `str` — UTF-8
- JSON-serializable types (`dict`, `list`, primitives) — `json.dumps` with sorted keys

For everything else, a `serializers` registry:

```python
configure(serializers={
    MyType: (serialize_fn, deserialize_fn),
})
```

Plugins extend this: `cairn[pydantic]` registers `BaseModel` serialization, `cairn[pickle]` adds pickle support.

### Typing

The decorator transforms the return type:

```python
P = ParamSpec("P")
R = TypeVar("R")

# _step: Callable[P, Awaitable[R]] → Callable[P, Handle[R]]
```

This works with pyright via `ParamSpec`.

The argument transformation (`T` → `T | Handle[T]`) is **not** expressible with current Python typing without a plugin. In v1, passing a `Handle[str]` where `str` is expected will produce a type checker warning. This is a known limitation — the runtime handles it correctly.

---

## 2. `Handle[T]`

A Handle is an awaitable reference to a running step's eventual result.

### Creation

A Handle is returned immediately when a `step`-decorated function is called. Under the hood it wraps an `asyncio.Task`.

```python
h = research("cat", spec)   # returns Handle[str] immediately
result = await h              # blocks until research completes
```

### Lifecycle events

- **Spawn** — emitted when the Handle is created (in `Handle.__init__`). Records: span ID, parent span ID, step name.
- **Join** — emitted when the Handle is awaited (in `Handle.__await__`). Records: span ID, awaiter's span ID.

This enables the UI to detect fan-out (multiple spawns without intervening joins) and fan-in (multiple joins).

### Passing Handles as arguments

A Handle can be passed directly to another `step`-decorated function:

```python
page = fetch(url)                # Handle[str]
links = extract_links(page)      # page is Handle[str], framework awaits it
summary = summarize(extract_content(page))  # chained
```

The receiving function's decorator resolves Handles before calling the body. The function body always sees `T`, never `Handle[T]`. Join events are emitted for each resolution, building the dependency graph in the trace.

### Task group and structured concurrency

Each `step` execution runs within an implicit task group (anyio `TaskGroup`). All child steps spawned within a parent belong to the parent's task group. The parent's span does not close until all children complete.

```python
@step
async def parent():
    h1 = child_a()    # spawned in parent's task group
    h2 = child_b()    # spawned in parent's task group
    return await h1    # h2 still running
    # parent's body returns, but span stays open until h2 completes
```

When someone awaits the parent's Handle, they get the return value once the body finishes. But the parent span in the trace doesn't close until all children are done. This prevents leaked tasks — no orphaned `asyncio.Task`s that run forever.

### API

```python
class Handle(Generic[T]):
    def __await__(self) -> Generator[Any, Any, T]:
        """Await the result. Emits a join event."""
        ...

    def cancel(self) -> None:
        """Cancel the underlying task."""
        ...

    def done(self) -> bool:
        """Check if the task has completed."""
        ...
```

Mirrors `anyio.abc.TaskStatus` / `asyncio.Task` where applicable.

---

## 3. `trace()`

### Signature

```python
def trace(message: str, **kwargs: Any) -> None
```

Formless. The core records `(timestamp, parent_span_id, message, kwargs)` and does not interpret any kwargs. This is an atomic event — no duration, no start/end.

### What it does

1. Read `_current_span` from contextvars to get the parent.
2. Emit a trace event to the log.
3. Append the trace record to the parent span's collected traces (for `cached_tracing()`).

### Usage patterns

```python
# Progress
trace("processing", progress=(3, 10))

# Status annotation
trace("waiting for rate limit slot", status="pending")
trace("calling API", status="running")

# Edge annotation (labels transition between child steps)
trace("retrying", edge=True, reason="validation failed")

# Arbitrary metadata
trace("checkpoint", model="gpt-4", tokens=1523)
```

The core ships no opinions about what kwargs mean. UI plugins interpret conventions:
- `progress` key → render a progress bar
- `status` key → color the node
- `edge=True` → label the edge between previous and next child step

### Typed extensions via plugins

The core `trace()` has `**kwargs: Any`. A UI plugin re-exports it with typed kwargs using PEP 692:

```python
# cairn_ui/trace.py
class UITraceKwargs(TypedDict, total=False):
    progress: tuple[int, int]
    status: str
    edge: bool

def trace(message: str, **kwargs: Unpack[UITraceKwargs]) -> None:
    _core_trace(message, **kwargs)
```

Import from the plugin for autocomplete; import from core for formless. Same function at runtime.

### Edge annotations

A `trace(..., edge=True)` between two child steps annotates the transition:

```python
result = await validate(spec, draft)    # child A ends
trace("retrying", edge=True)            # annotates A → B transition
draft = await refine(draft, feedback)   # child B starts
```

Only one `edge=True` trace between a pair of children. Multiple is an error. Traces without `edge=True` are plain events on the parent's timeline.

---

## 4. `cached_output()` and `cached_tracing()`

### `cached_output() -> T | None`

Returns the previous cached result for the current step invocation, or `None` if no cache entry exists.

Always available in any step, regardless of the `memo` setting:
- `memo=True` — the framework auto-returns the cached value before calling the body. The body is never called, so `cached_output()` is moot.
- `memo=False` — the framework calls the body. `cached_output()` returns the previous result if one exists.

### `cached_tracing() -> list[TraceRecord] | None`

Returns the trace events from the previous cached execution, with relative timestamps (deltas). Enables faithful replay:

```python
prev = cached_output()
traces = cached_tracing()
if prev is not None and traces is not None:
    for t in traces:
        await anyio.sleep(t.delta)
        trace(t.message, **t.kwargs)
    return prev
```

### What's stored in the cache

Each cache entry contains:
- `result` — the serialized return value
- `traces` — list of `TraceRecord(message, delta, kwargs)` from that execution
- `error` — the exception, if the step failed (stored for browsing, but not returned as a cache hit)

---

## 5. Event log

### Format

Append-only JSONL. One line per event. All events have a `ts` (monotonic timestamp) field.

### Event types

```
spawn   — Handle created. Fields: id, parent, name, identity, version
start   — Step body begins executing. Fields: id
end     — Step body completed. Fields: id, cached (bool, optional)
error   — Step body raised an exception. Fields: id, err
join    — Handle awaited. Fields: id, by
trace   — Annotation. Fields: parent, msg, **kwargs
```

### Context tracking

A `ContextVar[TaskSpan | None]` called `_current_span` tracks the active step. Set on step entry, reset on step exit. Child steps and `trace()` calls read it to determine their parent.

Since each `asyncio.Task` gets its own contextvars copy, concurrent steps have independent parent tracking. No locks needed.

### Example event log

For this code:

```python
@step
async def main():
    handles = [research(a) for a in ["cat", "dog"]]
    for h in handles:
        await h

@step
async def research(subject: str) -> str:
    trace("building prompt")
    return await claude(f"research {subject}")
```

The log:

```jsonl
{"e":"spawn","id":"1","name":"main","ts":0}
{"e":"start","id":"1","ts":1}
{"e":"spawn","id":"2","parent":"1","name":"research","ts":2}
{"e":"spawn","id":"3","parent":"1","name":"research","ts":3}
{"e":"start","id":"2","ts":4}
{"e":"start","id":"3","ts":5}
{"e":"trace","parent":"2","msg":"building prompt","ts":6}
{"e":"spawn","id":"4","parent":"2","name":"claude","ts":7}
{"e":"trace","parent":"3","msg":"building prompt","ts":8}
{"e":"spawn","id":"5","parent":"3","name":"claude","ts":9}
{"e":"start","id":"4","ts":10}
{"e":"start","id":"5","ts":11}
{"e":"end","id":"4","ts":2000}
{"e":"end","id":"2","ts":2001}
{"e":"join","id":"2","by":"1","ts":2002}
{"e":"end","id":"5","ts":3000}
{"e":"end","id":"3","ts":3001}
{"e":"join","id":"3","by":"1","ts":3002}
{"e":"end","id":"1","ts":3003}
```

The UI reconstructs the tree from `parent` pointers and renders:

```
main                        [0 ————————————————————— 3003]
├── research("cat")         [2 ——————————— 2001]
│   ├── "building prompt"       [6]
│   └── claude(...)         [7 ———————— 2000]
├── research("dog")         [3 ———————————————— 3001]
│   ├── "building prompt"       [8]
│   └── claude(...)         [9 —————————————— 3000]
```

Fan-out detected: two spawns from "1" before any joins.

---

## 6. Stores

### Output store

Content-addressed. Maps cache keys to serialized results.

```
.cairn/outputs/{cache_key_hash}
```

Each entry is an immutable blob containing the serialized result (or error), collected trace records, and execution metadata. The cache key is `sha256(canonical(identity, version, resolved_args))`.

Entries with errors are stored (for browsability) but treated as cache misses on re-execution — errors are retried, not replayed.

### Trace store (runs)

Per-run execution logs with symlinks into the output store.

```
.cairn/runs/
    main-2026-04-16T10:30:00/
        trace.jsonl
        001-research-cat → ../../outputs/a3f...
        002-claude-abc123 → ../../outputs/b7e...
        003-research-dog → ../../outputs/c91...
        004-claude-def456 → ../../outputs/d04...
    main/
        latest → ../main-2026-04-16T10:30:00
```

Key format: `{entry_point_id}-{datetime}`. Within a run: `{seqid}-{step_name}` symlinks, flat, sorted by execution order.

The `{entry_point_id}/latest` symlink always points to the most recent run for that entry point.

### Garbage collection

Nix-style: remove old run directories from `runs/`. Then sweep `outputs/` for blobs with no remaining symlinks pointing to them. A `cairn gc` command or programmatic API.

---

## 7. `run()` and CLI

### Programmatic entry point

```python
from cairn import run

run(
    main,                          # entry step function
    store=FileStore(".cairn"),      # cache backend (default: file-based)
    sink=JSONLSink(".cairn/runs"),  # trace log destination
)
```

`run()` sets up the event loop, initializes the output store and trace sink in contextvars, executes the entry step, waits for all tasks to complete, and exits.

### CLI entry point

```bash
cairn script.py                 # runs main() from script.py
cairn script.py:my_pipeline     # runs my_pipeline() from script.py
cairn script.py --replay        # swaps in replayable wrappers
```

Streamlit-style: launches a frontend (if a UI plugin is installed), runs the script, handles human interaction through the UI. Same runtime as `run()`, just with defaults wired up.

---

## 8. Higher-order patterns

These are **not** framework builtins. They are user-space functions built from the core primitives. The framework provides the building blocks; users compose them.

### Memoization (opt-in)

When `memo=True`, the decorator auto-short-circuits on cache hit. This is equivalent to:

```python
prev = cached_output()
if prev is not None:
    return prev
return await fn(*args, **kwargs)
```

### Replayable

Replays from cache with simulated timing. Indistinguishable from a live execution — no `cached: true` in the trace.

```python
def replayable(fn: Callable[P, Awaitable[R]]) -> Callable[P, Handle[R]]:
    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        prev = cached_output()
        traces = cached_tracing()
        if prev is not None and traces is not None:
            for t in traces:
                await anyio.sleep(t.delta)
                trace(t.message, **t.kwargs)
            return prev
        return await fn(*args, **kwargs)
    return step(
        wrapper, memo=False,
        identity=Identity.from_function(fn),
        version=Version.from_function(fn),
    )
```

### Rate limiting

```python
def rate_limited(n: int):
    sem = anyio.Semaphore(n)

    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Handle[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            trace("waiting for slot", status="pending")
            async with sem:
                trace("acquired slot", status="running")
                return await fn(*args, **kwargs)
        return step(
            wrapper, memo=True,
            identity=Identity.from_function(fn),
            version=Version.from_function(fn),
        )
    return decorator
```

### Retry

```python
def with_retry(max_attempts: int = 3):
    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Handle[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            for attempt in range(max_attempts):
                try:
                    trace(f"attempt {attempt + 1}", progress=(attempt + 1, max_attempts))
                    return await fn(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    trace("retrying", error=str(e))
        return step(
            wrapper, memo=True,
            identity=Identity.from_function(fn),
            version=Version.from_function(fn),
        )
    return decorator
```

### Validation loop

A higher-order pattern: research + validate + refine in a loop.

```python
async def validated(
    generate_fn,
    validate_fn,
    refine_fn,
    *args,
    max_retries: int = 3,
    **kwargs,
):
    draft = await generate_fn(*args, **kwargs)
    for i in range(max_retries):
        result = await validate_fn(draft)
        if result["success"]:
            return draft
        trace("retrying", edge=True, attempt=i + 1, feedback=result["feedback"])
        draft = await refine_fn(draft, result["feedback"])
    return draft
```

Used as:

```python
report = await validated(
    research, validate, refine,
    subject="cat", spec=spec,
)
```

### Human-in-the-loop

Human interaction is just a step that blocks until input arrives via an external harness:

```python
@step(memo=False)
async def human_review(question: str, prefill: str | None = None) -> str:
    prev = cached_output()
    return await harness.ask(question, prefill=prev or prefill)
```

The harness is injected via contextvar or constructor. It implements:

```python
class InputHarness(Protocol):
    async def ask(self, question: str, prefill: str | None = None) -> str: ...
    async def judge(self, item: str) -> Judgment: ...
```

Different harness implementations: web UI, TUI, Telegram bot, Slack, etc. Separate packages (`cairn-web`, `cairn-tui`).

---

## 9. Plugin architecture

### Serialization plugins (extras)

Lightweight. Shipped as optional dependencies of the main package.

```toml
# pyproject.toml
[project.optional-dependencies]
pydantic = ["pydantic>=2.0"]
pickle = []
all = ["cairn[pydantic]"]
```

Activated by explicit import:

```python
import cairn.ext.pydantic   # registers BaseModel hash_func + serializer
```

### Harness plugins (separate packages)

Heavier. Have their own dependencies and logic.

```
cairn-web        # web UI for trace visualization + human interaction
cairn-tui        # terminal UI
cairn-telegram   # Telegram bot harness
```

### UI trace extensions (re-exports)

UI plugins re-export `trace()` with typed kwargs:

```python
from cairn_web import trace   # typed: progress, status, edge
# vs
from cairn import trace       # formless: **Any
```

Same function at runtime, different types at check time.

---

## 10. Configuration

Global configuration via `configure()`:

```python
from cairn import configure

configure(
    hash_funcs={
        Path: lambda p: (str(p), p.stat().st_mtime) if p.exists() else str(p),
    },
    serializers={
        MyType: (serialize_fn, deserialize_fn),
    },
)
```

For v1, configuration is global (one process = one config). Internally backed by contextvars so it can become per-runtime in v2 without breaking the API.

---

## Appendix: pseudocode for core runtime

```python
_current_span: ContextVar[TaskSpan | None] = ContextVar('current_span', default=None)

def emit(event: dict):
    sink.write({**event, "ts": monotonic()})


class Handle(Generic[T]):
    def __init__(self, span: TaskSpan, task: asyncio.Task[T]):
        self._span = span
        self._task = task
        emit({"e": "spawn", "id": span.id, "parent": span.parent_id, "name": span.name})

    def __await__(self):
        awaiter = _current_span.get()
        emit({"e": "join", "id": self._span.id, "by": awaiter.id if awaiter else None})
        return self._task.__await__()

    def cancel(self):
        self._task.cancel()

    def done(self) -> bool:
        return self._task.done()


def step(fn=None, *, memo=False, identity=None, version=None):
    if fn is None:
        return functools.partial(step, memo=memo, identity=identity, version=version)

    _identity = identity or Identity.from_function(fn)
    _version = version or Version.from_function(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        parent = _current_span.get()
        span = TaskSpan(
            id=next_id(),
            parent_id=parent.id if parent else None,
            name=fn.__name__,
            identity=_identity,
            version=_version,
            raw_args=(args, kwargs),
        )

        async def run():
            token = _current_span.set(span)
            try:
                async with anyio.create_task_group() as tg:
                    span._task_group = tg

                    # resolve Handle arguments
                    resolved = {}
                    for k, v in span.bound_args.items():
                        resolved[k] = (await v) if isinstance(v, Handle) else v

                    # cache lookup
                    key = cache_key(_identity, _version, resolved)
                    cached = store.get(key)

                    if cached is not None:
                        span._cached_output = cached.result
                        span._cached_tracing = cached.traces
                        if memo:
                            emit({"e": "end", "id": span.id, "cached": True})
                            return cached.result

                    # execute
                    emit({"e": "start", "id": span.id})
                    result = await fn(**resolved)
                    emit({"e": "end", "id": span.id})

                    # store
                    store.put(key, CacheEntry(result, span._traces))
                    return result
                # task group exits here — waits for all children

            except Exception as exc:
                emit({"e": "error", "id": span.id, "err": str(exc)})
                raise
            finally:
                _current_span.reset(token)

        return Handle(span, asyncio.create_task(run()))

    wrapper.identity = _identity
    wrapper.version = _version
    return wrapper


def trace(message: str, **kwargs):
    parent = _current_span.get()
    emit({"e": "trace", "parent": parent.id if parent else None, "msg": message, **kwargs})
    if parent:
        parent._traces.append(TraceRecord(message, monotonic(), kwargs))


def cached_output():
    span = _current_span.get()
    return span._cached_output if span else None


def cached_tracing():
    span = _current_span.get()
    return span._cached_tracing if span else None
```
