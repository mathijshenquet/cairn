# TODO: Migrate to anyio TaskGroup.create_task()

## Current state

We use `asyncio.TaskGroup.create_task()` for structured concurrency. This works but locks us to asyncio.

## Upstream

anyio has merged `TaskGroup.create_task()` returning `TaskHandle[T]` on the master branch, but it's **unreleased** as of anyio 4.13.0. It should ship in a future release.

The `TaskHandle[T]` API provides:
- `await handle` / `handle.wait()` — get result
- `handle.cancel()` — cancel the task
- `handle.status` — PENDING, FINISHED, CANCELLING, CANCELLED, FAILED
- `handle.return_value` / `handle.exception`

This is almost identical to our `Handle[T]`, making migration straightforward.

Relevant upstream links:
- PR: https://github.com/agronholm/anyio/pull/1098 (merged 2026-04-14, unreleased as of 4.13.0)

## Migration plan

1. Wait for anyio release with `create_task()`
2. Replace `asyncio.TaskGroup` with `anyio.create_task_group()`
3. Replace `tg.create_task(coro)` with `tg.create_task(coro)` (same API)
4. Our `Handle[T]` wraps anyio's `TaskHandle[T]` instead of `asyncio.Task[T]`
5. Remove asyncio-only constraint from tests (enable trio backend)

## Impact

- Enables trio backend support
- Cleaner cancellation semantics
- No API changes for users
