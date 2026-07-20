"""Testes do FlareMiddleware sobre uma app Starlette real (via TestClient)."""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from flare_sdk.fastapi import FlareMiddleware
from tests.conftest import FakeClient


class _RecordingClient(FakeClient):
    """Cliente falso que grava as chamadas de ``request`` do middleware."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[dict] = []

    def request(self, method, path, status_code, *, duration_ms=None, **attrs):  # noqa: ANN001, ANN201
        self.requests.append(
            {
                "method": method,
                "path": path,
                "status_code": status_code,
                "duration_ms": duration_ms,
            }
        )
        return True


def _app(client: _RecordingClient, **mw_kwargs) -> Starlette:
    async def ok(request):  # noqa: ANN001, ANN201
        return PlainTextResponse("ok")

    async def show(request):  # noqa: ANN001, ANN201
        return PlainTextResponse("item")

    async def boom(request):  # noqa: ANN001, ANN201
        raise RuntimeError("kaboom")

    async def health(request):  # noqa: ANN001, ANN201
        return PlainTextResponse("healthy")

    app = Starlette(
        routes=[
            Route("/ok", ok),
            Route("/orders/{order_id}", show),
            Route("/boom", boom),
            Route("/health", health),
        ]
    )
    app.add_middleware(FlareMiddleware, client=client, **mw_kwargs)
    return app


def test_records_method_path_status_duration() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client)
    TestClient(app).get("/ok")

    (req,) = client.requests
    assert req["method"] == "GET"
    assert req["path"] == "/ok"
    assert req["status_code"] == 200
    assert req["duration_ms"] >= 0


def test_groups_by_route_template() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client)
    TestClient(app).get("/orders/42")

    # Agrupa por template, não pelo id concreto.
    assert client.requests[0]["path"] == "/orders/{order_id}"


def test_raw_path_when_grouping_disabled() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client, group_paths=False)
    TestClient(app).get("/orders/42")
    assert client.requests[0]["path"] == "/orders/42"


def test_ignored_paths_are_not_recorded() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client)
    TestClient(app).get("/health")
    assert client.requests == []


def test_exception_records_500_and_reraises() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client)
    with pytest.raises(RuntimeError, match="kaboom"):
        TestClient(app, raise_server_exceptions=True).get("/boom")

    (req,) = client.requests
    assert req["status_code"] == 500
    assert req["path"] == "/boom"
