"""
flare-sdk — cliente leve para enviar logs e requests ao Flare.
==============================================================

Exporta o essencial no topo do pacote para que a integração comum caiba em um
import::

    from flare_sdk import Flare, FlareHandler

O middleware de FastAPI mora em :mod:`flare_sdk.fastapi` (import separado) para não
tocar em ``starlette`` quem só quer o handler de log.
"""
from __future__ import annotations

from ._trace import get_trace_id, new_trace_id, reset_trace_id, set_trace_id
from ._transport import FlareTransportError, PermanentError, TransientError
from ._version import __version__
from .client import Flare
from .handler import FlareHandler

__all__ = [
    "Flare",
    "FlareHandler",
    "FlareTransportError",
    "PermanentError",
    "TransientError",
    # O elo entre log e request. `get_trace_id` é o que a app usa no dia a dia:
    # ela LÊ o id que o middleware criou para repeti-lo nas próprias mensagens.
    "get_trace_id",
    "new_trace_id",
    "reset_trace_id",
    "set_trace_id",
    "__version__",
]
