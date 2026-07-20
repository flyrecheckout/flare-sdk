"""
Exemplo mínimo: plugar o Flare no ``logging`` e mandar alguns eventos.

Rode com as variáveis de ambiente apontando para o seu Flare::

    export FLARE_TOKEN="seu-source-token"
    export FLARE_INGEST_URL="https://flare.lunacheckout.com/ingest"
    python examples/quickstart.py
"""
from __future__ import annotations

import logging

from flare_sdk import Flare, FlareHandler

# Um cliente compartilhado: serve o handler de log E as chamadas manuais.
flare = Flare(default_attributes={"service": "exemplo"})

logging.basicConfig(level=logging.INFO)
logging.getLogger().addHandler(FlareHandler(client=flare))

log = logging.getLogger("checkout")

# 1) Log estruturado — os campos do `extra` viram atributos no Flare.
log.info("pagamento aprovado", extra={"order_id": 42, "gateway": "pagarme"})

# 2) Um erro com stack — vai como severity ERROR e a stack num atributo.
try:
    1 / 0
except ZeroDivisionError:
    log.exception("falha ao calcular troco")

# 3) Envio manual de uma request (sem o middleware).
flare.request("POST", "/charge", 201, duration_ms=87.4, gateway="pagarme")

# Garante a entrega antes de o script morrer (num serviço longo, o atexit faz isso).
flare.flush(timeout=5)
flare.close()
print("Enviado. Veja em /logs e /requests no Flare.")
