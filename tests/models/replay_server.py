"""A real `/v1/completions` server that replays recorded vLLM responses.

This is the model layer's answer to a problem every other suite here solved by using the real
thing: `tests/db/` gets a real PostgreSQL, `tests/leanserv/` gets a real Lean kernel, and CI has
neither a GPU nor any way to run vLLM. The alternative to *some* stand-in is mocking the client,
which this repo does not do.

So the compromise is drawn one layer out. This is a genuine ASGI application implementing the
genuine endpoint, and the client under test makes a genuine HTTP request against it; what is
synthetic is only the token generation. The bytes it returns are not invented -- they were
recorded from a real vLLM 0.28.0 by `record_fixtures.py` and are replayed verbatim, so the schema
the client parses is one a real server actually emitted.

**A request with no recorded match is a loud 409, never a plausible default.** Serving a generic
response for an unmatched request is the failure this design exists to avoid: the client could
change what it sends -- drop `return_token_ids`, rename a sampling field -- and every test would
keep passing against an answer to a question nobody asked. Matching is exact on the request body,
which makes the fixture file a contract on the client's request shape: change the request, and the
tests fail until someone re-records against a real server and reads the diff.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

FIXTURE_PATH = Path(__file__).parent / "data" / "vllm_completions.json"


@dataclass(frozen=True)
class Interaction:
    name: str
    recorded: bool
    note: str
    request: dict[str, Any]
    status: int
    response: dict[str, Any]


@dataclass(frozen=True)
class Fixtures:
    provenance: dict[str, Any]
    interactions: tuple[Interaction, ...]

    def by_name(self, name: str) -> Interaction:
        for interaction in self.interactions:
            if interaction.name == name:
                return interaction
        raise KeyError(f"no recorded interaction named {name!r}")

    def match(self, body: dict[str, Any]) -> Interaction | None:
        """The first interaction whose recorded request is exactly `body`.

        Exact rather than "the important fields": see the module docstring. Two fixtures
        deliberately share a request (`greedy_single` and `greedy_single_repeat`, recorded to show
        the real server was deterministic), so first-match is the defined behaviour rather than an
        accident -- a test that needs a specific one of a pair asks `by_name`.
        """
        for interaction in self.interactions:
            if interaction.request == body:
                return interaction
        return None


def load_fixtures(path: Path = FIXTURE_PATH) -> Fixtures:
    document = json.loads(path.read_text())
    return Fixtures(
        provenance=document["provenance"],
        interactions=tuple(
            Interaction(
                name=i["name"],
                recorded=i["recorded"],
                note=i["note"],
                request=i["request"],
                status=i["status"],
                response=i["response"],
            )
            for i in document["interactions"]
        ),
    )


class RecordingTransport(httpx.AsyncBaseTransport):
    """Passes requests through to a real server and keeps every exchange, verbatim.

    The other way to record (M3.4's) is to rebuild each request with `CompletionsClient.build_body`
    and post it separately. That is enough when the prompt is a fixed id list; it is not enough
    once the prompt is *produced* -- a policy renders band 1, the pinned tokenizer turns it into
    ids, the router merges configured sampling with the policy's -- because a separate recorder
    would have to reproduce all of that and could drift from it. Recording through the transport
    the real pipeline used means the fixture holds exactly the bytes that pipeline sent, and the
    replay server's exact match then fails the moment any of those steps changes.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.exchanges: list[tuple[dict[str, Any], int, dict[str, Any]]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        self.exchanges.append((json.loads(request.content), response.status_code, json.loads(body)))
        # Rebuilt rather than returned: the body has been consumed, and the inner response's
        # framing headers (`transfer-encoding`, `content-length`) describe a stream that is gone.
        return httpx.Response(
            status_code=response.status_code,
            headers={"content-type": response.headers.get("content-type", "application/json")},
            content=body,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def write_fixtures(
    path: Path,
    *,
    provenance: dict[str, Any],
    exchanges: list[tuple[dict[str, Any], int, dict[str, Any]]],
    name: str,
    note: str,
) -> None:
    """Write recorded exchanges in the schema `load_fixtures` reads, every one `recorded: true`."""
    interactions = [
        {
            "name": name if len(exchanges) == 1 else f"{name}_{index}",
            "recorded": True,
            "note": note,
            "request": request,
            "status": status,
            "response": response,
        }
        for index, (request, status, response) in enumerate(exchanges)
    ]
    document = {"provenance": provenance, "interactions": interactions}
    path.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n")


def create_replay_app(fixtures: Fixtures | None = None) -> FastAPI:
    """An ASGI app serving `POST /v1/completions` from `fixtures`.

    A factory rather than a module-level app, for the reason `leanserv.api.create_app` is one: a
    test that wants a different fixture set should not have to reach into module state.
    """
    loaded = fixtures if fixtures is not None else load_fixtures()
    app = FastAPI(title="recorded vLLM replay")

    @app.post("/v1/completions")
    async def completions(request: Request) -> JSONResponse:
        body = await request.json()
        interaction = loaded.match(body)
        if interaction is None:
            # 409, not 404: the endpoint exists and the request is well-formed, it is the
            # *fixture set* that does not cover it. Distinguishable from the recorded 404 for an
            # unknown model, which is a real vLLM response a test asserts against.
            return JSONResponse(
                status_code=409,
                content={
                    "error": "no recorded interaction matches this request",
                    "sent": body,
                    "available": [
                        {"name": i.name, "request": i.request} for i in loaded.interactions
                    ],
                    "hint": (
                        "Matching is exact on the request body. If the client legitimately "
                        "changed what it sends, re-record against a real vLLM with "
                        "tests/models/record_fixtures.py rather than loosening the match."
                    ),
                },
            )
        return JSONResponse(status_code=interaction.status, content=interaction.response)

    @app.get("/v1/models")
    async def models() -> JSONResponse:
        """Enough of the real endpoint for a readiness check to work."""
        return JSONResponse(
            content={
                "object": "list",
                "data": [{"id": loaded.provenance["model_id"], "object": "model"}],
            }
        )

    return app
