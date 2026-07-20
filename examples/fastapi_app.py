"""
Exemplo de FastAPI instrumentada: middleware de request + logs no mesmo cliente.

    export FLARE_TOKEN="seu-source-token"
    export FLARE_INGEST_URL="https://flare.lunacheckout.com/ingest"
    uvicorn examples.fastapi_app:app --reload

Cada request vira uma linha em /requests; cada `log.*` vira uma linha em /logs.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from flare_sdk import Flare, FlareHandler
from flare_sdk.fastapi import FlareMiddleware

# Um cliente para tudo: middleware de request e handler de log dividem a fila.
flare = Flare(default_attributes={"service": "checkout-api"})

logging.getLogger().addHandler(FlareHandler(client=flare))
logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger("checkout")

app = FastAPI()
# Registra method/path/status/duração de cada request automaticamente.
app.add_middleware(FlareMiddleware, client=flare)


@app.get("/health")
async def health() -> dict:
    # Ignorado pelo middleware por padrão (rota de infra, alta frequência).
    return {"healthy": True}


@app.post("/orders/{order_id}/charge")
async def charge(order_id: int) -> dict:
    # Agrupado como /orders/{order_id}/charge nas métricas, não pelo id concreto.
    log.info("cobrança iniciada", extra={"order_id": order_id})
    return {"order_id": order_id, "status": "charged"}


@app.get("/boom")
async def boom() -> dict:
    # A request é registrada como 500 e a exceção sobe normalmente.
    raise RuntimeError("erro proposital para o exemplo")
