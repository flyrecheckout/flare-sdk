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
from typing import Any, Awaitable, Callable, Iterable, MutableMapping

from .client import Flare

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class FlareMiddleware:
    """Middleware ASGI que envia uma linha de request ao Flare por chamada HTTP."""

    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        *,
        client: Flare,
        group_paths: bool = True,
        ignore_paths: Iterable[str] = ("/health", "/metrics", "/ingest"),
    ) -> None:
        self.app = app
        self._client = client
        self._group_paths = group_paths
        # Rotas de infra (health, scrape de métrica) inundariam a telemetria com
        # ruído de alta frequência e zero valor de investigação.
        self._ignore = frozenset(ignore_paths)

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

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Registra o 500 e RE-LEVANTA: o middleware observa, não sequestra o erro.
            self._record(scope, method, path, status_holder["code"], start)
            raise
        else:
            self._record(scope, method, path, status_holder["code"], start)

    def _record(
        self, scope: Scope, method: str, path: str, status_code: int, start: float
    ) -> None:
        """Enfileira a request no Flare. ``client.request`` já é never-raise."""
        duration_ms = round((time.perf_counter() - start) * 1000, 3)
        self._client.request(
            method,
            self._route_template(scope) if self._group_paths else path,
            status_code,
            duration_ms=duration_ms,
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
