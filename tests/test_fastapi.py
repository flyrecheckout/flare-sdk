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
                "attrs": attrs,
            }
        )
        return True


def _app(client: _RecordingClient, **mw_kwargs) -> Starlette:
    from starlette.responses import JSONResponse, Response

    async def ok(request):  # noqa: ANN001, ANN201
        return PlainTextResponse("ok")

    async def show(request):  # noqa: ANN001, ANN201
        return PlainTextResponse("item")

    async def boom(request):  # noqa: ANN001, ANN201
        raise RuntimeError("kaboom")

    async def health(request):  # noqa: ANN001, ANN201
        return PlainTextResponse("healthy")

    async def echo(request):  # noqa: ANN001, ANN201
        # Lê o corpo (prova que o middleware NÃO consome à frente) e o devolve.
        body = await request.body()
        return JSONResponse({"received": body.decode("utf-8")})

    async def blob(request):  # noqa: ANN001, ANN201
        # Resposta binária: a captura deve IGNORAR pelo Content-Type.
        return Response(b"\x89PNG\r\n\x00\x01", media_type="image/png")

    async def secret(request):  # noqa: ANN001, ANN201
        # Devolve um header sensível (set-cookie) para provar a redação.
        return PlainTextResponse("ok", headers={"set-cookie": "sid=abc123"})

    app = Starlette(
        routes=[
            Route("/ok", ok),
            Route("/orders/{order_id}", show),
            Route("/boom", boom),
            Route("/health", health),
            Route("/echo", echo, methods=["POST"]),
            Route("/blob", blob),
            Route("/secret", secret),
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


# ── Captura de corpo (opt-in) ─────────────────────────────────────────────────


def test_bodies_not_captured_by_default() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client)  # sem os flags
    TestClient(app).post("/echo", content='{"a":1}')
    assert client.requests[0]["attrs"] == {}


def test_captures_request_and_response_bodies_when_enabled() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client, capture_request_body=True, capture_response_body=True)
    TestClient(app).post("/echo", content='{"order":42}',
                         headers={"content-type": "application/json"})

    attrs = client.requests[0]["attrs"]
    # A request foi lida pelo handler (echo) E capturada pelo middleware — a
    # observação não consome o corpo à frente.
    assert attrs["request_body"] == '{"order":42}'
    assert attrs["response_body"] == '{"received":"{\\"order\\":42}"}'


def test_capture_response_only_leaves_request_out() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client, capture_response_body=True)  # só a resposta
    TestClient(app).post("/echo", content='{"a":1}',
                         headers={"content-type": "application/json"})

    attrs = client.requests[0]["attrs"]
    assert "request_body" not in attrs
    assert "response_body" in attrs


def test_binary_response_body_is_skipped_by_content_type() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client, capture_response_body=True)
    TestClient(app).get("/blob")  # image/png
    assert "response_body" not in client.requests[0]["attrs"]


def test_response_body_is_capped_at_max_bytes() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client, capture_response_body=True, max_body_bytes=10)
    # /echo devolve um JSON maior que 10 bytes; a captura para no cap.
    TestClient(app).post("/echo", content='{"x":"aaaaaaaaaaaaaaaaaaaa"}',
                         headers={"content-type": "application/json"})
    assert len(client.requests[0]["attrs"]["response_body"]) == 10


# ── Captura de headers (opt-in) ───────────────────────────────────────────────


def test_headers_not_captured_by_default() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client)  # sem os flags
    TestClient(app).get("/ok", headers={"x-trace": "abc"})
    attrs = client.requests[0]["attrs"]
    assert "request_headers" not in attrs
    assert "response_headers" not in attrs


def test_captures_request_headers_when_enabled() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client, capture_request_headers=True)
    TestClient(app).get("/ok", headers={"x-trace": "abc"})

    headers = client.requests[0]["attrs"]["request_headers"]
    assert isinstance(headers, dict)
    # Nomes em minúsculas; o valor não-sensível passa cru.
    assert headers["x-trace"] == "abc"


def test_captures_response_headers_when_enabled() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client, capture_response_headers=True)
    TestClient(app).get("/ok")

    headers = client.requests[0]["attrs"]["response_headers"]
    assert isinstance(headers, dict)
    # A response do PlainTextResponse sempre carimba content-type.
    assert "content-type" in headers


def test_sensitive_request_header_is_redacted() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client, capture_request_headers=True)
    TestClient(app).get("/ok", headers={"authorization": "Bearer secret-token"})

    headers = client.requests[0]["attrs"]["request_headers"]
    # O nome fica (o operador vê que existe), mas o segredo é redigido.
    assert headers["authorization"] == "***"


def test_api_key_request_header_is_redacted() -> None:
    """O `api-key` (sem o prefixo x-) é credencial e NUNCA pode ir cru ao dashboard."""
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client, capture_request_headers=True)
    TestClient(app).get("/ok", headers={"api-key": "super-secret-key"})

    headers = client.requests[0]["attrs"]["request_headers"]
    assert headers["api-key"] == "***"
    assert "super-secret-key" not in str(client.requests[0]["attrs"])


def test_sensitive_response_header_is_redacted() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client, capture_response_headers=True)
    TestClient(app).get("/secret")

    headers = client.requests[0]["attrs"]["response_headers"]
    assert headers["set-cookie"] == "***"


def test_non_http_scopes_pass_through_untouched() -> None:
    from starlette.testclient import TestClient

    client = _RecordingClient()
    app = _app(client)
    # O `with` dispara o ciclo de lifespan (scope type != http): o middleware o
    # repassa intocado e não registra request nenhuma por causa dele.
    with TestClient(app):
        pass
    assert client.requests == []
