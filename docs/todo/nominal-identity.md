# TODO: Nominal identity & layered storage

Parked design direction from a longer discussion. The `own_time` / `own_size`
plumbing is orthogonal and being worked on now — see "Not parked" at the end.

## Thesis

Default identity is **nominal** (`(name, args)`), not intensional (source hash).

Reasons:

- Source hashing is a bad proxy for semantic dependency. Whitespace edits
  invalidate; functionally equivalent refactors invalidate; meanwhile a prompt
  swap or model switch buried in the function body silently *doesn't*
  invalidate. It catches cosmetic change and misses semantic change.
- Stochastic steps (LLM calls, human-in-the-loop) have no meaningful
  intensional identity. Their output isn't a function of their source — it's a
  function of model weights, prompt text, temperature, the person answering.
  Source-hashing them pretends otherwise.
- Deep call-tree hashing in Python is genuinely hard (dynamic dispatch, runtime
  step selection). Nominal sidesteps the problem.

Nominal forces behavior-affecting changes to surface as *data*: as arguments,
as inner `@step`s, or as explicitly declared deps. This is a discipline, but
it's honest.

## Where the distinction has teeth

Only at `memo=True`.

- `memo=False` always re-runs. Stale cache can only produce mismatched
  *recordings*, never wrong *results*. Nominal is unambiguously correct here.
- `memo=True` skips execution on a cache hit. Missing deps = silent stale
  output. This is where discipline and tooling should concentrate.

`memo=True` is already an opt-in "I want to skip this" decision, so it's also
the right moment to be asking "what are my deps?"

## Layered store

Today `outputs/{key}.json` conflates value bytes and call-level metadata.
Split three ways under the "a run is pure" framing:

- **L0 — CAS.** Pure value store. Bytes by content hash. Universal.
- **L1 — nominal memo index.** Per observed call:
  `(name, args) → (output_hash, trace_hash, ast_hash, size, own_size, time, own_time, memo_flag, child_l1_keys)`.
  For `memo=True`: skip execution on hit. For `memo=False`: back
  `cached_output()` as a hint. Same data, different policy.
- **L2 — runs.** Tree of `(call_path → L1_key)` plus run-level metadata
  (entry, timestamp, trace). UX/observability scaffolding only. A run is a
  *cursor* into the universal graph, not an owner of outputs.

This makes `cached_output()` natural: today it feels weird because "the run
owns the cache." Under "L1 is global, runs are views," it's just a lookup into
the global memoization scoped to the current `(name, args)`.

GC follows the layering: L2 = roots; L1 GC'd when no L2 (and no L1 parent via
`child_l1_keys`) references it; L0 GC'd when no L1 references it. Refcounting
three deep, no special cases.

## Per-step cache key

Make it explicit:

```python
@step                                     # default: ("name", "args")
@step(cache_key=("kripkean", "args"))     # rename-tolerant
@step(cache_key=("name", "version", "args"))  # pure-compute opt-in
```

Each tuple encodes a *bet about purity* the user makes at the call site.

## Deps declaration (for `memo=True`)

Primary patterns, in preference order:

1. **Lift to args.** `download_paper(url, model="haiku")`. The model is now
   in the cache key for free. Most explicit.
2. **`@step` the inner thing.** `render_prompt` becomes its own step; its
   output is an arg to the LLM step, which keys on it. Uses the framework's
   own primitive.
3. **`deps=` escape hatch.** `@step(deps=lambda: {"model": MODEL_NAME})`
   for closure-captured constants you don't want to reshape.

Explicitly avoid: autodetecting closure captures, auto-tracking module
globals. Brittle, surprising, undoes the win of "deps are explicit."

## Defensive AST fingerprint

Store `ast_hash(fn)` on every L1 write (memo=True *and* False). On read,
recompute and compare; emit a warning event on mismatch. Never affects
correctness — smoke detector, not circuit breaker.

Extension: also hash statically-resolvable inner `@step`s the function calls,
so warnings can be precise ("your source is unchanged but `render_prompt`
drifted since this recording"). Strong hint, not a guarantee.

Also useful in debug-from-trace: show per-node "source has drifted N edits
since this trace was recorded."

## Equivalence (`eq=`)

For regression / behavior tests:

```python
@step(eq=semantic_eq)            # LLM outputs
@step(eq=numeric_close(rtol=1e-3))
```

Default `==`. The "relaxed compatibility" use case is just a non-default `eq`.
Composes with fixtured L1 entries: look up by `(name, args)`, compare recorded
vs. fresh via the comparator.

## Kripkean aliases

`aliases.json` at the store root, maps retired names → kripkean UUID.

Mandatory once nominal is default. A rename would otherwise shatter every
cache entry and every fixture. Intensional identity failed at rename too, but
was already invalidating on most edits, so the pain was less visible.

## Flows this unlocks

### Debug-from-trace

Edit code → invalidate a subtree of a recorded run → replay → diff per
call-path. Requires `child_l1_keys` to walk upward (evict the node and every
caller that consumed its output) and the AST warning to surface stale hits
from code you forgot to edit.

Alternative to automatic upward invalidation: a `cairn invalidate <run> <node>`
CLI that walks the L1 graph explicitly. Manual but predictable.

### Regression / behavior tests

A frozen L1 keyed by `("name"|"kripkean", "args")` *is* the fixture DB — no
new storage primitive. On upgrade, run, check `eq` on recorded vs. fresh.
"Compatibility" is just a non-default `eq`.

### Global shared cache (the reference-checker story)

Shared L0 + L1, nominal key, so impl tweaks don't evict. Trust model
(signing, per-user namespaces) is a separate, deferred problem.

## Open bets / unresolved

- **Trace pointer impurity.** `memo=True` + nondeterministic function means
  the trace from the first observation is frozen forever under
  `cached_tracing()`. Default bet: first observation wins. Emit a second
  warning class ("L1 key overwritten with different output") when a second
  observation would have produced different bytes. Cheap to detect, surfaces
  the impurity the cache is silently flattening.
- **Missing-dep failure mode.** Silent staleness from an undeclared dep.
  Defenses: AST fingerprint warning, a `cairn doctor` lint over `memo=True`
  bodies flagging non-trivial module-level references, a dev-mode trace view
  surfacing the effective cache-key inputs.
- **Cross-module AST fingerprinting.** Scope v1 to statically-resolvable
  inner `@step` calls. Dynamic dispatch (e.g. picking a step from a dict at
  runtime) is out of reach without observed-call-tree recording.
- **L0 sharding of compound outputs.** Future: when a function returns a
  list/dict, shard it into multiple L0 entries so dedup works at the element
  level. Changes the `own_size` calculation but not the API.

## Not parked — doing now

`own_time` and `own_size` are orthogonal plumbing, land first:

- `own_time = end_ts - start_ts - sum(time suspended awaiting child handles)`.
  Pure `TaskSpan` lifecycle. No identity/layer dependencies.
- `own_size == size` until L0 dedup exists. Today: the bytes written for this
  entry. The `Store.put → (size, own_size)` API survives unchanged when L0
  arrives.
- Emit both on the existing `end` event. No sidecar metadata file yet — that
  belongs with the L1 split above.
