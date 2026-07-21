"""Versão do pacote, num só lugar.

Isolada num módulo próprio para que ``__init__`` a importe sem carregar mais nada,
e para que o build (hatchling) e o ``User-Agent`` do transporte leiam a mesma fonte.
"""
from __future__ import annotations

__version__ = "0.4.0"
