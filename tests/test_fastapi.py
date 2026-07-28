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
    # O `trace_id` NÃO é captura opcional: ele é o elo entre esta request e os logs
    # dela, e vai sempre (ver `trace=`). O que este teste guarda é o corpo — que
    # continua ausente sem os flags.
    attrs = client.requests[0]["attrs"]
    assert set(attrs) == {"trace_id"}
    assert "request_body" not in attrs and "response_body" not in attrs


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


# ── Flush por request (serverless) ────────────────────────────────────────────
# O bug que estes testes guardam: em Lambda o container congela ao fim da
# invocação, e o reflexo é escrever um `@app.middleware("http")` que chama
# `flare.flush()` depois do `call_next`. Só que o `call_next` do BaseHTTPMiddleware
# retorna no `http.response.start` — ANTES de o corpo ser transmitido e, portanto,
# antes de o FlareMiddleware gravar a request. O flush encontra a fila sem o evento
# atual, e a request mais recente nunca chega ao dashboard.


class _FlushingClient(_RecordingClient):
    """Grava, a cada flush, QUANTAS requests já haviam sido enfileiradas.

    É essa contagem que prova a ordem: com o flush no lugar certo, a request atual
    já está na fila quando ele roda (>= 1). Contar só "flush foi chamado" passaria
    igual com o flush cedo demais — que é exatamente o bug.
    """

    def __init__(self) -> None:
        super().__init__()
        self.flushes: list[int] = []

    def flush(self, timeout=None):  # noqa: ANN001, ANN201
        self.flushes.append(len(self.requests))
        return True


def test_no_flush_per_request_by_default() -> None:
    """Servidor de longa duração: o envio é em background, sem bloquear a resposta."""
    from starlette.testclient import TestClient

    client = _FlushingClient()
    TestClient(_app(client)).get("/ok")

    assert client.flushes == []


def test_flush_after_request_runs_after_the_request_is_queued() -> None:
    """A ordem é o ponto: quando o flush roda, a request atual JÁ está na fila."""
    from starlette.testclient import TestClient

    client = _FlushingClient()
    TestClient(_app(client, flush_after_request=True)).get("/ok")

    assert len(client.requests) == 1
    assert client.flushes == [1]  # 1 == a request desta chamada já enfileirada


def test_flush_after_request_also_runs_when_the_app_raises() -> None:
    """No caminho de erro o lote também precisa sair — é o evento mais importante."""
    from starlette.testclient import TestClient

    client = _FlushingClient()
    app = _app(client, flush_after_request=True)
    with pytest.raises(RuntimeError):
        TestClient(app, raise_server_exceptions=True).get("/boom")

    assert client.flushes == [1]


def test_a_failing_flush_never_breaks_the_response() -> None:
    """Instrumentação não derruba a app: um flush que estoura é engolido."""
    from starlette.testclient import TestClient

    class _Boom(_RecordingClient):
        def flush(self, timeout=None):  # noqa: ANN001, ANN201
            raise RuntimeError("rede caiu no flush")

    client = _Boom()
    response = TestClient(_app(client, flush_after_request=True)).get("/ok")

    assert response.status_code == 200
    assert len(client.requests) == 1


def test_flush_after_request_survives_an_outer_base_http_middleware() -> None:
    """A montagem real do Lambda: um BaseHTTPMiddleware por fora do FlareMiddleware.

    É a combinação que produzia o bug. Com o flush DENTRO do middleware, a ordem se
    mantém mesmo com o `call_next` do outro retornando cedo.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.testclient import TestClient

    client = _FlushingClient()
    app = _app(client, flush_after_request=True, capture_response_body=True)

    async def passthrough(request, call_next):  # noqa: ANN001, ANN201
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
    TestClient(app).post("/echo", content='{"a":1}',
                         headers={"content-type": "application/json"})

    assert client.flushes == [1]
    # E o corpo continua capturado — o flush no lugar certo não o atropela.
    assert client.requests[0]["attrs"]["response_body"] == '{"received":"{\\"a\\":1}"}'


# ── O elo entre log e request: o trace_id ────────────────────────────────────


def test_the_request_row_carries_a_trace_id() -> None:
    """A RAZÃO DE O CAMPO EXISTIR. Sem ele a linha de request e as linhas de log da
    mesma chamada ficam lado a lado no Flare sem nada que as ligue, e o dashboard
    não consegue afirmar qual request produziu qual erro."""
    from starlette.testclient import TestClient

    client = _RecordingClient()
    TestClient(_app(client)).get("/")

    trace = client.requests[0]["attrs"]["trace_id"]
    assert trace and len(trace) == 32  # uuid4().hex


def test_the_app_reads_the_same_id_the_request_row_gets() -> None:
    """A DIREÇÃO do contrato: o middleware ESCREVE, a app LÊ. É o que permite ao
    `logger.error` de dentro do endpoint sair com o mesmo id que foi para a coluna
    da request — sem isso, log e request teriam ids diferentes e o elo seria uma
    coincidência, não uma garantia."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from flare_sdk import get_trace_id

    visto: list = []

    async def endpoint(request):  # noqa: ANN001, ANN201
        visto.append(get_trace_id())
        return PlainTextResponse("ok")

    client = _RecordingClient()
    app = Starlette(routes=[Route("/x", endpoint)])
    app.add_middleware(FlareMiddleware, client=client)
    TestClient(app).get("/x")

    assert visto[0] == client.requests[0]["attrs"]["trace_id"]


def test_two_requests_do_not_share_a_trace() -> None:
    """Duas chamadas são duas transações. Um id compartilhado amarraria o erro de
    uma ao caminho da outra — o pior tipo de dado: plausível e errado."""
    from starlette.testclient import TestClient

    client = _RecordingClient()
    cliente_http = TestClient(_app(client))
    cliente_http.get("/")
    cliente_http.get("/")

    assert client.requests[0]["attrs"]["trace_id"] != client.requests[1]["attrs"]["trace_id"]


def test_an_id_already_in_context_wins() -> None:
    """Quem já definiu o trace está afirmando de que transação esta chamada faz
    parte (um id vindo do gateway, de uma fila). Gerar outro por cima partiria o
    rastro em dois."""
    from starlette.testclient import TestClient

    from flare_sdk import reset_trace_id, set_trace_id

    token = set_trace_id("id-de-fora")
    try:
        client = _RecordingClient()
        TestClient(_app(client)).get("/")
    finally:
        reset_trace_id(token)

    assert client.requests[0]["attrs"]["trace_id"] == "id-de-fora"


def test_the_trace_does_not_leak_to_the_next_request() -> None:
    """O escopo fecha no fim da chamada, inclusive quando a app estoura. Sem isso,
    num servidor que reaproveita a task, o id de uma request apareceria na
    seguinte."""
    from starlette.testclient import TestClient

    from flare_sdk import get_trace_id

    client = _RecordingClient()
    with pytest.raises(RuntimeError):
        TestClient(_app(client)).get("/boom")

    assert get_trace_id() is None


def test_trace_off_leaves_the_column_null() -> None:
    """`trace=False` para quem já gerencia o próprio contexto (OpenTelemetry). Aí o
    campo fica ausente — NULL na coluna — em vez de um id que não corresponde a
    rastro nenhum."""
    from starlette.testclient import TestClient

    client = _RecordingClient()
    TestClient(_app(client, trace=False)).get("/")

    assert "trace_id" not in client.requests[0]["attrs"]
