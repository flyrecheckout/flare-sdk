"""
FlareMiddleware — instrumenta requests de qualquer app ASGI (FastAPI/Starlette).
================================================================================

Um middleware ASGI puro: mede cada request e manda ``method``/``path``/
``status_code``/``duration_ms`` ao Flare. Não importa ``starlette`` nem ``fastapi``
— fala o protocolo ASGI direto —, então acrescentá-lo não puxa dependência nova
para quem já tem a app.

Instalação (uma linha)::

    from flare_sdk import Flare
    from flare_sdk.fastapi import FlareMiddleware

    flare = Flare(token="...", endpoint="https://flare.example.com/ingest")
    app.add_middleware(FlareMiddleware, client=flare)

Corpo da request/response (opt-in)
----------------------------------
Com ``capture_request_body`` / ``capture_response_body`` o middleware anexa o corpo
como os atributos ``request_body`` / ``response_body`` — as chaves que a tela de
detalhe do Flare mostra nas abas Request/Response ("o que foi enviado / o que foi
devolvido"). O corpo é **capado** em ``max_body_bytes`` e só capturado se o
``Content-Type`` for textual (JSON, texto, form) — binário (imagem, octet-stream)
é ignorado. ⚠️ O corpo pode conter dado sensível (PII, tokens): ligue por rota/app
com consciência, é OFF por padrão.

Headers da request/response (opt-in)
------------------------------------
Com ``capture_request_headers`` / ``capture_response_headers`` o middleware anexa os
headers como os atributos ``request_headers`` / ``response_headers`` — cada um um
dict ``{nome: valor}`` (nomes em minúsculas, valores decodificados latin-1).
Também OFF por padrão. Headers sensíveis (``authorization``,
``proxy-authorization``, ``cookie``, ``set-cookie``, ``x-api-key``,
``x-auth-token``) têm o VALOR redigido para ``"***"`` — o nome permanece para o
operador ver que o header existe, mas o segredo não vaza para o dashboard.
``Authorization`` carrega o Bearer; capturar sem redigir vazaria a credencial.

Duas decisões de desenho
------------------------
* **Só observa, nunca interfere.** Se a app levanta, o middleware registra a request
  como 500 e **re-levanta** a exceção original — instrumentar não pode engolir o
  erro que o cliente precisa ver.
* **Agrupa por rota, não por URL.** Com ``group_paths`` (default), usa o template
  da rota (``/orders/{id}``) em vez do path concreto (``/orders/42``). Sem isso,
  cada id vira um "endpoint" diferente e a tela de métricas explode em cardinalidade.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Iterable, MutableMapping, Optional

from .client import Flare

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

#: Content-Types cujo corpo vale a pena capturar como texto. Binário fora daqui
#: (imagem, octet-stream) viraria lixo com caracteres de substituição — não se
#: captura. Um Content-Type ausente é tratado como textual (best-effort): a maioria
#: das APIs JSON o define, e o que não define costuma ser texto simples.
_TEXTUAL_PREFIXES = (
    "application/json",
    "application/xml",
    "application/xhtml",
    "application/x-www-form-urlencoded",
    "text/",
)

#: Headers cujo VALOR é segredo: redigidos para ``"***"`` na captura. O nome fica
#: (o operador vê que o header existe), mas o conteúdo não. ``Authorization`` e
#: ``x-api-key`` carregam credencial; ``cookie``/``set-cookie`` carregam sessão —
#: capturar sem redigir vazaria tudo isso para o dashboard.
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
    }
)


def _headers_dict(headers: Any) -> dict:
    """Lista ASGI de headers ``[(bytes, bytes)]`` → dict ``{nome: valor}``.

    Nome em minúsculas (o ASGI já entrega assim, mas normaliza-se por garantia) e
    valores/nomes decodificados latin-1 (o charset do protocolo HTTP). Header
    sensível tem o valor trocado por ``"***"`` — ver :data:`_SENSITIVE_HEADERS`.
    """
    result: dict[str, str] = {}
    for key, value in headers or []:
        name = key.decode("latin-1", "replace").lower()
        if name in _SENSITIVE_HEADERS:
            result[name] = "***"
        else:
            result[name] = value.decode("latin-1", "replace")
    return result


def _content_type(headers: Any) -> str:
    """O ``content-type`` (sem os parâmetros após ``;``), em minúsculas, ou ``""``."""
    for key, value in headers or []:
        if key.lower() == b"content-type":
            return value.decode("latin-1", "replace").split(";")[0].strip().lower()
    return ""


def _is_textual(content_type: str) -> bool:
    """O corpo desse ``content-type`` é texto capturável? Ausente conta como sim."""
    if not content_type:
        return True
    return any(content_type.startswith(prefix) for prefix in _TEXTUAL_PREFIXES)


def _decode_body(buffer: bytearray, content_type: str) -> Optional[str]:
    """Bytes acumulados → texto, ou ``None`` se vazio ou não-textual.

    ``errors="replace"`` porque um corte no meio de um caractere multibyte (o cap
    de tamanho não respeita fronteira de UTF-8) não pode derrubar a captura — um �
    é melhor que uma exceção que perde o evento inteiro.
    """
    if not buffer or not _is_textual(content_type):
        return None
    return bytes(buffer).decode("utf-8", errors="replace") or None


class FlareMiddleware:
    """Middleware ASGI que envia uma linha de request ao Flare por chamada HTTP."""

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        *,
        client: Flare,
        group_paths: bool = True,
        ignore_paths: Iterable[str] = ("/health", "/metrics", "/ingest"),
        capture_request_body: bool = False,
        capture_response_body: bool = False,
        capture_request_headers: bool = False,
        capture_response_headers: bool = False,
        max_body_bytes: int = 16384,
    ) -> None:
        self.app = app
        self._client = client
        self._group_paths = group_paths
        # Rotas de infra (health, scrape de métrica) inundariam a telemetria com
        # ruído de alta frequência e zero valor de investigação.
        self._ignore = frozenset(ignore_paths)
        self._capture_request = capture_request_body
        self._capture_response = capture_response_body
        self._capture_request_headers = capture_request_headers
        self._capture_response_headers = capture_response_headers
        self._max_body = max(0, max_body_bytes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Só HTTP interessa: lifespan e websocket passam intocados.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._ignore:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        # 500 é o default: se a app estourar antes de mandar o start, a request foi
        # um erro não-tratado — e é exatamente isso que o status deve refletir.
        status_holder = {"code": 500}
        request_body = bytearray()
        response_body = bytearray()
        response_ct = {"value": ""}
        # Os headers da response só existem no ``http.response.start`` — guarda-se a
        # lista crua aqui para o ``_record`` montar o dict depois.
        response_headers = {"value": None}

        async def receive_wrapper() -> MutableMapping[str, Any]:
            # Observa o corpo da request enquanto ele PASSA para a app — sem consumir
            # à frente (a app ainda recebe cada mensagem), então não há replay.
            message = await receive()
            if (
                self._capture_request
                and message["type"] == "http.request"
                and len(request_body) < self._max_body
            ):
                chunk = message.get("body", b"")
                request_body.extend(chunk[: self._max_body - len(request_body)])
            return message

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            mtype = message["type"]
            if mtype == "http.response.start":
                status_holder["code"] = message["status"]
                if self._capture_response:
                    response_ct["value"] = _content_type(message.get("headers"))
                if self._capture_response_headers:
                    response_headers["value"] = message.get("headers")
            elif (
                mtype == "http.response.body"
                and self._capture_response
                and len(response_body) < self._max_body
            ):
                chunk = message.get("body", b"")
                response_body.extend(chunk[: self._max_body - len(response_body)])
            await send(message)

        # Só embrulha o receive quando há razão — a captura de request é o único
        # caso que precisa dele; senão passa o original e evita uma indireção.
        inner_receive = receive_wrapper if self._capture_request else receive

        start = time.perf_counter()
        try:
            await self.app(scope, inner_receive, send_wrapper)
        except Exception:
            # Registra o 500 e RE-LEVANTA: o middleware observa, não sequestra o erro.
            self._record(scope, method, path, status_holder["code"], start,
                         request_body, response_body, response_ct["value"],
                         response_headers["value"])
            raise
        else:
            self._record(scope, method, path, status_holder["code"], start,
                         request_body, response_body, response_ct["value"],
                         response_headers["value"])

    def _record(
        self,
        scope: Scope,
        method: str,
        path: str,
        status_code: int,
        start: float,
        request_body: bytearray,
        response_body: bytearray,
        response_ct: str,
        response_headers: Any,
    ) -> None:
        """Enfileira a request no Flare. ``client.request`` já é never-raise."""
        duration_ms = round((time.perf_counter() - start) * 1000, 3)
        attributes: dict[str, Any] = {}
        if self._capture_request:
            text = _decode_body(request_body, _content_type(scope.get("headers")))
            if text is not None:
                attributes["request_body"] = text
        if self._capture_response:
            text = _decode_body(response_body, response_ct)
            if text is not None:
                attributes["response_body"] = text
        if self._capture_request_headers:
            attributes["request_headers"] = _headers_dict(scope.get("headers"))
        if self._capture_response_headers:
            attributes["response_headers"] = _headers_dict(response_headers)
        self._client.request(
            method,
            self._route_template(scope) if self._group_paths else path,
            status_code,
            duration_ms=duration_ms,
            **attributes,
        )

    @staticmethod
    def _route_template(scope: Scope) -> str:
        """Reconstrói o template da rota (``/orders/{id}``) a partir do path casado.

        O Starlette não expõe o template pronto no scope — só ``endpoint`` e
        ``path_params`` (ex.: ``{'order_id': '42'}``). Então remonta-se: cada
        segmento do path cujo valor casa um path param vira ``{chave}``. É
        best-effort (um valor que coincida com um segmento estático viraria
        placeholder), mas cobre o caso comum e é o que segura a cardinalidade da
        tela de métricas. Sem path params (rota estática ou 404), devolve o path cru.
        """
        path = scope.get("path", "")
        params = scope.get("path_params") or {}
        if not params:
            return path
        by_value = {}
        for key, value in params.items():
            by_value.setdefault(str(value), key)
        segments = [
            "{" + by_value[seg] + "}" if seg in by_value else seg
            for seg in path.split("/")
        ]
        return "/".join(segments)
