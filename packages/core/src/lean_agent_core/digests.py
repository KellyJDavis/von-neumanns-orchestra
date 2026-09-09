"""Content addresses that identify *what was proved*, as opposed to which row proved it.

Separate from `blobs.py` (which addresses opaque byte content) and from `leanserv`'s
`compute_cache_key` (which addresses a check *request*): those two answer "have I stored these
exact bytes" and "have I run this exact check". This module answers "is this the same goal",
which is a different question with different consumers -- the seal endpoint stamps it onto every
obligation it creates, and spec §5.2's cycle guard compares it across a decomposition DAG.
"""

from __future__ import annotations

import hashlib


def compute_goal_digest(base_env_digest: bytes, goal_src: str) -> bytes:
    """`obligation.goal_digest` (spec §5.2: "content address of the sealed goal; NOT identity").

    Over the goal's own statement source and the environment it was elaborated in, and
    deliberately **not** over its declaration name. The name is generated per obligation, so
    including it would make every goal's digest unique -- which would silently disable spec
    §5.2's cycle guard ("a child's `goal_digest` may not equal any ancestor's"), the one place
    that stops a policy decomposing an obligation into itself and consuming budget forever. The
    guard needs two spellings of the same goal to collide, so identity-bearing fields must stay
    out.

    The base env is included because the same source text is a different goal against a different
    environment: notation, instances, and even the meaning of a bare identifier all come from the
    imports, so `∀ n, n + 0 = n` over `Init` and over Mathlib are not interchangeable.

    Digests the statement *source*, not its elaborated form. Two goals that differ only in
    notation or bound-variable names therefore do not collide even though they are the same
    proposition -- so this is sound for the cycle guard (no false "same goal", which would
    wrongly reject a legitimate decomposition) but incomplete (a cycle spelled two different ways
    is not caught here, and is caught instead by `run.max_depth`, which spec makes the
    unconditional backstop). Digesting a canonical elaborated form would tighten this and needs a
    Lean-side canonicalization to exist first; it is not something to fake in Python.
    """
    return hashlib.sha256(base_env_digest + hashlib.sha256(goal_src.encode()).digest()).digest()


def compute_bundle_digest(bundle_source: str) -> bytes:
    """Names spec §4.1's generated bundle file, `LeanAgent/Goals/Bundle_<digest>.lean`.

    Distinct from `obligation.sealed_olean_sha`, which is the digest of what compiling this
    source *produces*: the bundle is sealed the moment it is elaborated, but its `.olean` is
    built lazily out of band, so the source digest is the only one available on the hot path.
    """
    return hashlib.sha256(bundle_source.encode()).digest()
