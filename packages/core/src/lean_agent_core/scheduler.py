"""Claim, lease, heartbeat and reaper (spec §6.4).

Same division as `state.py`: the database holds the invariants, this module drives them. `claim`
and `reap` are `SECURITY DEFINER` functions in `deploy/grants.sql` because both write
`obligation.status`, which `app` cannot; the heartbeat is a plain `UPDATE` on `attempt`, which
`app` holds outright.

The one piece of real machinery here is `HeartbeatThread`, and it is a thread rather than an
asyncio task on purpose -- see its own docstring. Everything else is a single function call.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Self

from sqlalchemy import Engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: Spec §6.4's own numbers: a 60 s lease refreshed every 20 s, so a live worker misses three beats
#: before it can be reaped. Both are overridable per call; the ratio is what matters, since a
#: refresh interval close to the lease turns ordinary scheduling jitter into spurious reaping.
DEFAULT_LEASE = timedelta(seconds=60)
DEFAULT_HEARTBEAT_INTERVAL = timedelta(seconds=20)


@dataclass(frozen=True)
class ClaimedAttempt:
    """A claim that succeeded: the obligation is now `in_progress` and this attempt owns its
    lease. `None` from `claim_attempt` is the ordinary idle case, not an error."""

    attempt_id: uuid.UUID
    obligation_id: uuid.UUID
    run_id: uuid.UUID


async def claim_attempt(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worker_id: str,
    policy_id: str,
    policy_config_hash: bytes,
    lease: timedelta = DEFAULT_LEASE,
    eligible_tenants: list[uuid.UUID] | None = None,
) -> ClaimedAttempt | None:
    """Claim the highest-priority schedulable obligation and open an attempt on it.

    Returns `None` when nothing is claimable -- no open obligation with budget left in a running
    run. A caller backs off and retries; spec's control loop does exactly that.

    One database round trip, because the whole select-lock-update-insert chain has to be one
    statement: a claim that chose an obligation in one round trip and marked it in another would
    hand the same work to two workers. The function returns the attempt's row rather than its id
    for the same reason -- reading the row back with a second query would be a second round trip
    into a table another worker is concurrently writing.

    `eligible_tenants` is spec's `$eligible_tenants`, which "the admission loop" is supposed to
    refresh. That loop is post-MVP (§9), so `None` means no tenant filter -- carried through as an
    explicit parameter rather than assumed, so the shape is right when the loop arrives.
    """
    async with session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT attempt_id, obligation_id, run_id FROM "
                    "claim_attempt(:worker, :lease, :policy, :cfg_hash, :tenants)"
                ),
                {
                    "worker": worker_id,
                    "lease": lease,
                    "policy": policy_id,
                    "cfg_hash": policy_config_hash,
                    "tenants": eligible_tenants,
                },
            )
        ).one_or_none()
        await session.commit()
    if row is None:
        return None
    return ClaimedAttempt(attempt_id=row[0], obligation_id=row[1], run_id=row[2])


async def reap_expired_attempts(
    session_factory: async_sessionmaker[AsyncSession], *, limit: int = 100
) -> int:
    """Expire attempts whose lease has lapsed and return their obligations to `open`, uncharged.

    Returns how many were reaped. Spec: "Lease expiry means *the worker is gone*, never *the
    attempt took too long*" -- so this is an infrastructure sweep, and charging the obligation for
    it would let a node that keeps dying quietly consume every obligation's budget.

    Batched and best-effort (`SKIP LOCKED`, a `limit`): a reaper that aborted on one odd row would
    stop reclaiming everything behind it.
    """
    async with session_factory() as session:
        reaped = (
            await session.execute(text("SELECT reap_expired_attempts(:limit)"), {"limit": limit})
        ).scalar_one()
        await session.commit()
    return int(reaped)


class HeartbeatThread:
    """Keeps one attempt's lease alive for as long as the worker is alive.

    **A dedicated thread with its own connection, deliberately not an asyncio task.** Spec's
    reasoning, which is the whole justification for the extra machinery: "a blocking tokenizer
    call or CPU-bound serialization inside a policy would otherwise starve it and get a live
    worker reaped." An asyncio heartbeat only runs when the event loop is free, and a policy that
    spends thirty seconds inside a synchronous call gives it no chance to -- the worker is
    perfectly healthy and gets reaped anyway, its work discarded, for a reason no log would
    explain. A thread is scheduled by the OS and keeps beating through exactly that.

    Session-level advisory locks would also detect a dead worker, and spec rejects them for a
    reason worth not rediscovering: they "would pin one backend connection per in-flight attempt,
    which with hour-long attempts caps concurrency at `max_connections` to buy seconds of
    detection latency."

    Each beat extends the lease as well as stamping `heartbeat_at` -- stamping alone would leave
    the lease expiring under a worker that is demonstrably alive.

    `lost_lease` becomes true if a beat finds the attempt no longer claimed or running: something
    else (the reaper, most likely) has already taken it away, and whatever this worker is doing is
    now orphaned. Recorded rather than raised, because the thread has no sensible way to interrupt
    the work; the control loop checks it and abandons its result.
    """

    def __init__(
        self,
        engine: Engine,
        attempt_id: uuid.UUID,
        *,
        every: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
        lease: timedelta = DEFAULT_LEASE,
    ) -> None:
        self._engine = engine
        self._attempt_id = attempt_id
        self._every = every
        self._lease = lease
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost_lease = False
        self.beats = 0

    def beat_once(self) -> bool:
        """One heartbeat, synchronously. Returns whether the attempt still holds its lease.

        Public because a test (and, plausibly, a worker wanting a beat at a known moment) needs to
        drive exactly one beat rather than wait out an interval.
        """
        with self._engine.connect() as conn:
            result = conn.execute(
                text(
                    "UPDATE attempt SET heartbeat_at = now(), lease_expires_at = now() + :lease "
                    "WHERE id = :id AND status IN ('claimed', 'running')"
                ),
                {"id": self._attempt_id, "lease": self._lease},
            )
            conn.commit()
        held = result.rowcount == 1
        if held:
            self.beats += 1
        else:
            self.lost_lease = True
        return held

    def _run(self) -> None:
        # `Event.wait` rather than `sleep`: a worker finishing in under one interval should not
        # have to wait out the remainder of it before its thread joins.
        while not self._stop.wait(self._every.total_seconds()):
            if not self.beat_once():
                return

    def __enter__(self) -> Self:
        # One beat before the thread starts, so the lease is freshly extended by the time the
        # caller begins work rather than only after the first interval elapses.
        self.beat_once()
        self._thread = threading.Thread(
            target=self._run, name=f"heartbeat-{self._attempt_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
