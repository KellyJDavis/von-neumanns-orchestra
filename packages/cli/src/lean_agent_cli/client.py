"""httpx client for the public API (spec §6.1).

Synchronous, because a CLI is: every command is one request-and-print, and an event loop would buy
nothing while making `Ctrl-C` and tracebacks worse.

Takes an optional `httpx.Client` so a caller can supply its own transport -- which is also how the
tests point it straight at the ASGI app, so the code a user runs is the code under test rather
than a parallel adapter.
"""

from __future__ import annotations

import uuid
from types import TracebackType
from typing import Any, Self

import httpx

#: Generous, because `POST /v1/runs` does real work before it answers: it elaborates the
#: submission, seals every goal and compiles the bundle (M2.6/M2.7). A default that timed out
#: mid-materialization would leave a run whose obligations exist and whose bundle does not, which
#: is exactly the state M2.7 exists to prevent anyone being in.
DEFAULT_TIMEOUT = httpx.Timeout(600.0, connect=10.0)


class ApiError(RuntimeError):
    """A non-2xx response, carrying the server's own `detail` rather than a status code alone --
    the API's 404s and 409s say *why* (no such base env, run not running, nothing to assemble), and
    discarding that in favour of "HTTP 409" would make the CLI strictly less useful than curl."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class ApiClient:
    def __init__(self, base_url: str = "", *, client: httpx.Client | None = None) -> None:
        if client is None and not base_url:
            raise ValueError("either `base_url` or `client` must be given")
        self._client = client or httpx.Client(base_url=base_url, timeout=DEFAULT_TIMEOUT)
        self._owned = client is None

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", response.text))
            except ValueError:
                detail = response.text
            raise ApiError(response.status_code, detail)
        return response.json()

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", "/v1/runs", json=payload))

    def get_trajectory(self, attempt_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("GET", f"/v1/attempts/{attempt_id}/trajectory"))

    def get_run(self, run_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("GET", f"/v1/runs/{run_id}"))

    def get_manifest(self, run_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("GET", f"/v1/runs/{run_id}/manifest"))

    def get_artifact(self, run_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("GET", f"/v1/runs/{run_id}/artifact"))

    def cancel_run(self, run_id: uuid.UUID | str) -> dict[str, Any]:
        return dict(self._request("POST", f"/v1/runs/{run_id}/cancel"))

    def list_obligations(
        self, run_id: uuid.UUID | str, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Follows `next_cursor` to exhaustion rather than returning one page.

        A CLI asking "what is in this run" wants the answer, not the first hundred rows -- and the
        API's keyset paging makes following the cursor correct even while the run is still being
        written to, which is exactly when someone runs `status`.
        """
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        out: list[dict[str, Any]] = []
        while True:
            page = self._request("GET", f"/v1/runs/{run_id}/obligations", params=params)
            out.extend(page["obligations"])
            if page["next_cursor"] is None:
                return out
            params["cursor"] = page["next_cursor"]
