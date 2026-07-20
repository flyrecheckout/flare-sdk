"""
Dublês e fixtures compartilhados. Nenhum teste toca a rede.
===========================================================

O transporte real (``urllib``) é substituído por :class:`FakeTransport`, que grava
os lotes numa lista em vez de mandar HTTP. É o que deixa a suíte exercitar a fila,
o batch e o retry de verdade — com threads reais — sem um servidor de pé.
"""
from __future__ import annotations

import threading
from typing import Any, List, Mapping, Sequence

import pytest


class FakeTransport:
    """Transporte que registra os lotes enviados, opcionalmente falhando.

    ``fail_with`` (uma exceção) faz todo ``send`` levantar — para exercitar retry e
    o caminho de erro. ``block`` (um Event) segura o ``send`` até ser liberado —
    para forçar, de forma determinística, a fila encher.
    """

    def __init__(
        self,
        *,
        fail_with: Exception | None = None,
        fail_times: int = 0,
        block: threading.Event | None = None,
        started: threading.Event | None = None,
    ) -> None:
        self.batches: List[List[dict]] = []
        self.calls = 0
        self._fail_with = fail_with
        # ``fail_times`` falha as N primeiras chamadas (com ``fail_with``) e depois
        # deixa passar — para exercitar "retentou e conseguiu".
        self._fail_times = fail_times
        self._block = block
        self._started = started
        self._lock = threading.Lock()

    def send(self, events: Sequence[Mapping[str, Any]]) -> int:
        with self._lock:
            self.calls += 1
            call_number = self.calls
        if self._started is not None:
            self._started.set()
        if self._block is not None:
            self._block.wait(timeout=5)
        if self._fail_times and call_number <= self._fail_times:
            raise self._fail_with or Exception("falha transitória")
        if self._fail_times == 0 and self._fail_with is not None:
            raise self._fail_with
        batch = [dict(e) for e in events]
        with self._lock:
            self.batches.append(batch)
        return len(batch)

    @property
    def sent_events(self) -> List[dict]:
        """Todos os eventos enviados, achatados na ordem de chegada."""
        flat: List[dict] = []
        for batch in self.batches:
            flat.extend(batch)
        return flat


class FakeClient:
    """Cliente falso para os testes do handler: só grava o que foi capturado."""

    def __init__(self) -> None:
        self.events: List[dict] = []
        self.closed = False

    def capture(self, event: Mapping[str, Any]) -> bool:
        self.events.append(dict(event))
        return True

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def env_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Popula ``FLARE_TOKEN``/``FLARE_INGEST_URL`` para o construtor não reclamar."""
    monkeypatch.setenv("FLARE_TOKEN", "test-token")
    monkeypatch.setenv("FLARE_INGEST_URL", "https://flare.test/ingest")
