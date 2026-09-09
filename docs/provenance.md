# Provenance policy

Decision record for spec §7.1. Written down in Phase 0 because it constrains schema and executor
design from the start — it is not something Phase 1+ code should be free to reinterpret.

## The constraint

The binding constraint on open-weights output is the **provenance of the training data**, not code
licensing. A trajectory produced by a symbolic tactic portfolio (zero model tokens) is unencumbered
training data; a trajectory produced by distilling a closed model is not, however permissively the
resulting weights are licensed.

## Enforcement, encoded in the schema and executor — not left to caller discipline

- `trajectory.provenance` is `NOT NULL`, with **no default**. A missing provenance value is a
  schema violation, not a value to backfill later.
- `provenance` is derived from `ModelBackend.provenance` at model registration time. It is never
  asserted by whatever code is writing the trajectory row — the caller does not get to declare its
  own provenance.
- The `symbolic` class is refused by the executor on any attempt whose completion count is nonzero.
  A model-guided tactic *choice* is not a symbolic trajectory, however few tokens that choice cost —
  provenance is about whether a model touched the trajectory at all, not about token volume.
- The corpus exporter **raises** on any trajectory outside `{open_weights, symbolic, human}` — it
  does not filter such trajectories out silently. A `closed_api_eval_only` trajectory reaching the
  exporter is a bug to fix, not data to quietly drop from the export.

## Why this matters enough to fix in Phase 0

`SymbolicPortfolio` (spec §6.6) is valuable beyond serving as a baseline: it is the system's source
of unencumbered training data at zero token cost. Bootstrapping capability by distilling a closed
model produces encumbered weights — a decision that is expensive to walk back once a corpus has
been collected under it, which is why the classification rules above are fixed now rather than
being left as a Phase 3+ concern.
