"""`LeanService` over HTTP (spec §6.2's internal API), for whatever runs a worker.

`lean_agent_core.protocols.LeanService` is the protocol a policy executor calls; this is the
implementation that talks to a real `leanserv` process. It lives here rather than in `core` for
the same reason `LinkOutcome` is mirrored rather than imported: `leanserv` already depends on
`core`, the wire format is this package's own, and `core` should not grow an `httpx` dependency to
describe something it only consumes through a protocol.

Until now the only implementation was a test adapter, which meant the code a deployment would
actually run had no coverage at all. `tests/leanserv/test_lean_service_client.py` exercises this
class against the real app over `httpx.ASGITransport` -- real client code, real server, no socket.

The integration suites still carry their own adapter. Consolidating them onto this class is
worthwhile and is deliberately not bundled here: those tests drive it from inside `asyncio.run`
bodies that would each need an extra `async with` scope to own the transport, and a refactor of
three integration files does not belong in the same change as the client they would use.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from types import TracebackType
from typing import Any, Self

import httpx
from lean_agent_core.enums import VerdictKind
from lean_agent_core.protocols import (
    CheckOutcome,
    DecomposedLemma,
    DecomposeOutcome,
    LeanService,
    LinkOutcome,
    SealedGoal,
    SealGoalRequest,
    SealOutcome,
)

#: leanserv's own per-command default is 300 s (spec §6.2), and a client that gave up sooner would
#: turn a slow-but-succeeding check into an `infra_error` -- the one outcome spec insists must not
#: be confused with a proof failure. The margin is for the HTTP round trip and the worker
#: acquisition that may precede the check itself.
DEFAULT_HTTP_TIMEOUT = httpx.Timeout(330.0, connect=10.0)


class LeanServiceClient:
    """HTTP client for `/v1/check`, `/v1/seal`, `/v1/decompose` and `/v1/link`.

    Accepts a caller-supplied `httpx.AsyncClient` so a deployment can bring its own transport,
    retry policy and connection limits -- and so tests can hand over one wired straight to the ASGI
    app, which is what makes the shipped code path the tested one.

    Deliberately does **not** retry. A `/v1/link` that timed out may still have written its
    verdict, and `verdict.attempt_id` is a primary key -- a blind retry would either collide or
    silently produce a second attempt's worth of kernel time against one attempt's budget. Retry
    policy belongs with the control loop, which knows whether a new attempt is the right answer.
    """

    def __init__(self, base_url: str = "", *, client: httpx.AsyncClient | None = None) -> None:
        if client is None and not base_url:
            raise ValueError("either `base_url` or `client` must be given")
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_HTTP_TIMEOUT)
        self._owned = client is None

    async def aclose(self) -> None:
        """Only closes a client this object created. A caller that supplied its own transport
        keeps ownership of it -- closing someone else's connection pool from here would break
        every other user of it."""
        if self._owned:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(path, json=payload)
        response.raise_for_status()
        return dict(response.json())

    async def check(
        self,
        *,
        base_env_digest: str,
        body: str,
        bundle_sha: str | None = None,
        timeout_ms: int | None = None,
    ) -> CheckOutcome:
        payload: dict[str, Any] = {"base_env_digest": base_env_digest, "body": body}
        if bundle_sha is not None:
            payload["bundle_sha"] = bundle_sha
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms
        data = await self._post("/v1/check", payload)
        return CheckOutcome(
            ok=data["ok"],
            diagnostics=tuple(data["diagnostics"]),
            cache_hit=data["cache_hit"],
            elapsed_ms=data["elapsed_ms"],
            axioms=tuple(data.get("axioms", ())),
        )

    async def seal(
        self,
        *,
        base_env_digest: str,
        goals: Sequence[SealGoalRequest],
        timeout_ms: int | None = None,
    ) -> SealOutcome:
        payload: dict[str, Any] = {
            "base_env_digest": base_env_digest,
            "goals": [
                {"name": g.name, "statement": g.statement, "level_params": list(g.level_params)}
                for g in goals
            ],
        }
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms
        data = await self._post("/v1/seal", payload)
        return SealOutcome(
            ok=data["ok"],
            goals=tuple(
                SealedGoal(
                    decl_name=g["decl_name"],
                    goal_src=g["goal_src"],
                    goal_digest=g["goal_digest"],
                    level_params=tuple(g["level_params"]),
                    diagnostics=tuple(g["diagnostics"]),
                    ok=g["ok"],
                )
                for g in data["goals"]
            ),
            bundle_source=data["bundle_source"],
            bundle_digest=data["bundle_digest"],
        )

    async def decompose(
        self, *, base_env_digest: str, development: str, timeout_ms: int | None = None
    ) -> DecomposeOutcome:
        payload: dict[str, Any] = {
            "base_env_digest": base_env_digest,
            "development": development,
        }
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms
        data = await self._post("/v1/decompose", payload)
        return DecomposeOutcome(
            ok=data["ok"],
            lemmas=tuple(
                DecomposedLemma(
                    name=lemma["name"],
                    statement=lemma["statement"],
                    level_params=tuple(lemma["level_params"]),
                    round_trips=lemma["round_trips"],
                    diagnostics=tuple(lemma["diagnostics"]),
                )
                for lemma in data["lemmas"]
            ),
            reassembly=data["reassembly"],
            diagnostics=tuple(data["diagnostics"]),
        )

    async def link(
        self,
        *,
        attempt_id: uuid.UUID,
        obligation_id: uuid.UUID,
        base_env_digest: str,
        bundle_sha: str,
        goal: str,
        entry: str,
        development: str,
        timeout_ms: int | None = None,
    ) -> LinkOutcome:
        payload: dict[str, Any] = {
            "attempt_id": str(attempt_id),
            "obligation_id": str(obligation_id),
            "base_env_digest": base_env_digest,
            "bundle_sha": bundle_sha,
            "goal": goal,
            "entry": entry,
            "development": development,
        }
        if timeout_ms is not None:
            payload["timeout_ms"] = timeout_ms
        data = await self._post("/v1/link", payload)
        return LinkOutcome(
            kind=VerdictKind(data["kind"]),
            link_ok=data["link_ok"],
            replay_ok=data["replay_ok"],
            axiom_audit_ok=data["axiom_audit_ok"],
            axioms=tuple(data["axioms"]),
            diagnostics=tuple(data["diagnostics"]),
            elapsed_ms=data["elapsed_ms"],
        )


def _conforms_to_lean_service_protocol() -> LeanService:
    """Structural conformance, checked by `mypy --strict` rather than asserted in prose -- the
    same device `SymbolicPortfolio` uses for `Policy`, and for the same reason: without a typed use
    inside `packages/`, drift between protocol and implementation would surface only at the
    executor's first call."""
    return LeanServiceClient(base_url="http://leanserv")
