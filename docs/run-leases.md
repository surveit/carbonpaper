# Run execution leases

A run outlives the process executing it only in persisted state. So a record saying
`running` cannot, by itself, say whether anyone is still executing it — and every
recovery decision depends on that answer.

## The lease

`app/core/run_lease.py` owns the vocabulary — the `RunLease` record and the
`RunLeaseStore` protocol. `app/runtime/lease.py` claims a lease before a run executes and
releases it after. `app/core/sqlite_store.py` implements the protocol, because
`tests/arch/` seals it as the only module that may import `sqlite3`, and because the fence
below needs the lease and the document in one transaction on one connection.

| field | meaning |
|---|---|
| `run_id` | the run's manifest id — the primary key, which is what makes a claim atomic |
| `executor_id` | which process holds it, for reading back; not a uniqueness claim |
| `fence` | rises on every claim, never on a renewal, so it names one executor's **tenure** |
| `expires_at` | unix seconds, always computed by SQLite |

`claim_lease` is a single `INSERT … ON CONFLICT DO UPDATE … WHERE expires_at <=
unixepoch() RETURNING …`. The insert wins when no row exists, the update fires only when
the stored tenure has expired, and the statement returns nothing when neither happened.
Two racing claimants cannot both receive a lease.

Every deadline is set and tested by SQLite, so executors are never compared against
their own hosts' clocks.

## Why a fence, given a lease

A lease alone would need the losing executor to cooperate — to notice it lost and stop.
A process wedged by a GC pause or CPU starvation notices nothing: its heartbeat thread
and any self-termination it might run are stalled together. From outside, its lease
expires and a successor takes over correctly. Then it wakes and writes.

`write_if_held` closes that: it reads `run_lease` and writes `documents` under one lock
on one connection, and returns `False` when the caller's fence has been superseded. Being
wrong about liveness stays survivable rather than becoming corruption.

It tests the fence and deliberately not the clock. An expired tenure nobody has claimed
has no rival writer, so refusing its holder would stop a process from recording what it is
still doing — including a process on its way down. What must be refused is a *superseded*
tenure, and a claim is the only thing that supersedes one.

`write_manifest` applies the fence to every run-record write, reading the held lease from
a `ContextVar` rather than taking it as an argument. Nothing outside a production run
binds one, so eval runs and stage tests write exactly as they did before.

## Heartbeat

The heartbeat renews on its own thread every 20 seconds against a 90-second TTL. Its own
thread matters: progress reaches the manifest only when a row finishes, so a stage waiting
out a rate limit can be honestly quiet for an hour, and a liveness signal driven by the
work would read that as death.

The TTL is long deliberately. A short lease costs a false takeover of a *healthy* run; a
long one costs only how late a dead run is noticed, and the fence bounds the damage of
being late.

## Why the sweep is periodic, not a boot hook

A boot cannot tell a run that *this* process orphaned from one a live peer is executing.
Both look identical: a record saying `running` beside a lease that has not yet expired.
Reading a boot as proof that the holder is gone is only sound if one process could ever be
executing, which is an assumption about the deployment rather than about the run.

So expiry is the only proof, and expiry arrives long after any boot — which makes a
boot-only sweep nearly useless, since the run a deploy just killed still holds a lease with
most of its TTL left. `watch_for_interrupted_runs` sweeps at boot and then every
`SWEEP_EVERY_SECONDS`, so a tenure is noticed whenever it lapses.

## Shutdown closes the common case immediately

Waiting out the TTL is correct but slow, and the overwhelmingly common interruption is a
deploy — where the process *knows* it is going away. `end_tenures_on_shutdown`, called from
the lifespan's shutdown path, expires the leases this process holds instead of deleting
them: expired means "restartable now", deleted would mean "never proven dead".

An ungraceful death still falls back to the TTL. The fast path is an optimisation over the
sound one, never a replacement for it.

## What the sweep does

`app/services/run_recovery.py` sorts every `running` record into three cases.

| the record's lease | what it means | what happens |
|---|---|---|
| live | someone may still be executing it | left alone |
| expired | its executor is provably gone | restarted, one at a time |
| absent | it predates leasing; nothing proved its executor died | logged by name, left for a human |

Restarts are serialized on one thread. A run re-executes only the stages that had not
finished, and the row cache spares the rows that had — a run killed part-way through a
model-backed stage re-spends only the rows that were in flight.

`fence` doubles as the tenure count. A run that kills its own process — an OOM on a large
frame — would otherwise restart into the same crash on every boot, spending a model call
each time, so past `MAX_TENURES` the run is abandoned and the stage that died carries the
reason. That is the only place recovery writes a terminal status: silence there would
leave the page spinning forever.
