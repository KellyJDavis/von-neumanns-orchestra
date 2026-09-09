"""M2.1.1: `lean_agent_core.digests` -- the content addresses that identify a sealed goal and its
bundle. Pure functions over bytes, so no Lean toolchain or Postgres is involved here (unlike
`tests/leanserv/test_api.py`, which exercises the same digests through the real `/v1/seal`).

What these assert is what the *consumers* need, not the particular hash construction: the cycle
guard needs two spellings of the same goal to collide and different goals not to, and the bundle
file name needs to address exactly the source it names.
"""

from __future__ import annotations

import hashlib

from lean_agent_core.digests import compute_bundle_digest, compute_goal_digest

BASE_A = bytes.fromhex("aa" * 32)
BASE_B = bytes.fromhex("bb" * 32)


def test_goal_digest_is_stable_for_the_same_goal() -> None:
    assert compute_goal_digest(BASE_A, "True") == compute_goal_digest(BASE_A, "True")


def test_goal_digest_distinguishes_statements() -> None:
    assert compute_goal_digest(BASE_A, "True") != compute_goal_digest(BASE_A, "False")


def test_goal_digest_distinguishes_base_environments() -> None:
    """The same source text over different imports is a different goal: notation, instances, and
    even what a bare identifier resolves to all come from the environment."""
    assert compute_goal_digest(BASE_A, "True") != compute_goal_digest(BASE_B, "True")


def test_goal_digest_takes_no_declaration_name() -> None:
    """A regression guard with teeth rather than a tautology: the signature itself is the
    property. `obligation.decl_name` is generated per obligation, so if it were ever folded into
    this digest, spec §5.2's cycle guard ("a child's `goal_digest` may not equal any ancestor's")
    would silently never match again -- every goal would be unique by construction.
    """
    from inspect import signature

    assert list(signature(compute_goal_digest).parameters) == ["base_env_digest", "goal_src"]


def test_bundle_digest_addresses_the_source_it_names() -> None:
    source = "import Init\nnamespace LeanAgent.Goals\nend LeanAgent.Goals\n"
    assert compute_bundle_digest(source) == hashlib.sha256(source.encode()).digest()


def test_bundle_digest_changes_when_a_goal_is_dropped() -> None:
    """A bundle assembled from only the goals that sealed is a different artifact than one
    including a goal that did not -- so `Bundle_<digest>.lean` names them apart."""
    full = "import Init\ndef G_a : Sort _ := True\ndef G_b : Sort _ := False\n"
    partial = "import Init\ndef G_a : Sort _ := True\n"
    assert compute_bundle_digest(full) != compute_bundle_digest(partial)
