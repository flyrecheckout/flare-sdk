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
    "__version__",
]
