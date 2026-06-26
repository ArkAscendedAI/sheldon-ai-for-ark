"""Per-connection write-serialization locks (H1 multi-user fix).

The DBManager caches ONE aiosqlite connection per cluster (campaign.db) and per
server (telemetry.db), shared by every concurrent player turn AND the background
census. aiosqlite runs each connection on its own worker thread, so individual
statements don't truly overlap — but coroutines still INTERLEAVE at every `await`.
Under the default implicit-transaction mode that means two multi-statement write
units on the same connection splice into one transaction: one coroutine's `commit()`
flushes the other's half-written work, and a `rollback()` rolls back whichever
transaction happens to be open (often the wrong one). That is exactly the torn
`dino_index` / premature-commit race (H1).

The fix: every WRITE unit acquires THIS connection's lock and holds it from its first
statement through `commit()`/`rollback()`, so no other writer can slip a statement in
between. Reads do NOT take the lock (they don't open a transaction; a read that briefly
observes an in-progress write self-heals on the next read, and telemetry is disposable).

The lock is keyed weakly by the connection object, so:
  * the SAME shared connection always yields the SAME lock (real serialization), and
  * the lock dies with the connection (no leak), and tests that build their own
    connections get correct per-connection serialization for free.

`conn_write_lock` is synchronous and never awaits, so it can't itself interleave —
the get-or-create of a connection's lock is atomic on the single event loop.
"""

from __future__ import annotations

import asyncio
import functools
import weakref

# conn -> its write-serialization lock. WeakKeyDictionary so entries vanish when the
# connection is garbage-collected (e.g. a DBManager.close() drops its last reference).
_locks: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def conn_write_lock(conn) -> asyncio.Lock:
    """Return the asyncio.Lock that serializes write units on `conn` (lazily created)."""
    lock = _locks.get(conn)
    if lock is None:
        lock = asyncio.Lock()
        _locks[conn] = lock
    return lock


def serialized_write(method):
    """Decorator for a store write-method: run the whole call holding `self.c`'s write-lock.

    Wrapping the entire method (its statements + commit()/rollback()) keeps a read-modify-write
    unit — e.g. the census upsert_region's SELECT→DELETE→INSERT→commit — atomic against any other
    writer sharing the same connection. The store methods never call one another, so the
    non-reentrant lock can't self-deadlock. Reads are intentionally left undecorated.
    """
    @functools.wraps(method)
    async def wrapper(self, *args, **kwargs):
        async with conn_write_lock(self.c):
            return await method(self, *args, **kwargs)
    return wrapper
