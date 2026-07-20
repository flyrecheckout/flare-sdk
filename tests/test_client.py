"""Testes do cliente: fila, batch, retry, never-raise, flush/close, fork."""
from __future__ import annotations

import threading

import pytest

from flare_sdk._transport import PermanentError, TransientError
from flare_sdk.client import Flare
from tests.conftest import FakeTransport


def _client(transport: FakeTransport, **kwargs) -> Flare:
    """Cria um Flare com credenciais fixas e injeta um transporte falso.

    O transporte é trocado antes do primeiro ``capture`` — o worker só nasce no
    primeiro evento, então nunca chega a usar o transporte real.
    """
    kwargs.setdefault("flush_interval", 0.05)
    kwargs.setdefault("retry_backoff", 0.0)
    flare = Flare(token="tok", endpoint="https://flare.test/ingest", **kwargs)
    flare._transport = transport
    return flare


def test_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLARE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token"):
        Flare(endpoint="https://flare.test/ingest")


def test_missing_endpoint_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLARE_INGEST_URL", raising=False)
    with pytest.raises(ValueError, match="endpoint"):
        Flare(token="tok")


def test_reads_credentials_from_env(env_credentials: None) -> None:
    flare = Flare()  # sem argumentos: vem do ambiente
    flare.close()


def test_log_is_delivered(fake_transport: FakeTransport) -> None:
    flare = _client(fake_transport)
    flare.log("hello", severity="INFO", order_id=7)
    assert flare.flush(timeout=2) is True
    flare.close()

    (event,) = fake_transport.sent_events
    assert event["message"] == "hello"
    assert event["severity"] == "INFO"
    assert event["order_id"] == 7
    assert "dt" in event


def test_request_marks_kind(fake_transport: FakeTransport) -> None:
    flare = _client(fake_transport)
    flare.request("POST", "/charge", 201, duration_ms=12.5)
    flare.flush(timeout=2)
    flare.close()

    (event,) = fake_transport.sent_events
    assert event["_kind"] == "request"
    assert event["method"] == "POST"
    assert event["status_code"] == 201
    assert event["duration_ms"] == 12.5


def test_default_attributes_merged(fake_transport: FakeTransport) -> None:
    flare = _client(fake_transport, default_attributes={"service": "checkout"})
    flare.log("x")
    flare.flush(timeout=2)
    flare.close()
    assert fake_transport.sent_events[0]["service"] == "checkout"


def test_batches_multiple_events(fake_transport: FakeTransport) -> None:
    flare = _client(fake_transport, batch_size=10)
    for i in range(5):
        flare.log(f"m{i}")
    flare.flush(timeout=2)
    flare.close()
    # Cinco eventos, entregues (um ou mais lotes) — a contagem total é o que importa.
    assert len(fake_transport.sent_events) == 5


def test_permanent_error_is_dropped_without_retry() -> None:
    errors: list[Exception] = []
    transport = FakeTransport(fail_with=PermanentError("403"))
    flare = _client(transport, on_error=errors.append)
    flare.log("x")
    flare.flush(timeout=2)
    flare.close()
    # Uma única tentativa (sem retry) e o erro reportado.
    assert transport.calls == 1
    assert len(errors) == 1


def test_transient_error_is_retried_then_reported() -> None:
    errors: list[Exception] = []
    transport = FakeTransport(fail_with=TransientError("503"))
    flare = _client(transport, max_retries=2, on_error=errors.append)
    flare.log("x")
    flare.flush(timeout=2)
    flare.close()
    # 1 tentativa + 2 retries = 3 chamadas, depois desiste e reporta.
    assert transport.calls == 3
    assert len(errors) == 1


def test_capture_after_close_returns_false(fake_transport: FakeTransport) -> None:
    flare = _client(fake_transport)
    flare.close()
    assert flare.capture({"message": "x"}) is False


def test_close_is_idempotent(fake_transport: FakeTransport) -> None:
    flare = _client(fake_transport)
    flare.log("x")
    flare.close()
    flare.close()  # não levanta


def test_context_manager_closes(fake_transport: FakeTransport) -> None:
    with _client(fake_transport) as flare:
        flare.log("x")
    assert flare._closed is True


def test_full_queue_drops_events() -> None:
    # Segura o worker no primeiro send para a fila encher de forma determinística.
    started = threading.Event()
    release = threading.Event()
    transport = FakeTransport(block=release, started=started)
    flare = _client(transport, batch_size=1, max_queue=2)

    flare.log("m0")           # worker pega este e bloqueia no send
    assert started.wait(timeout=2)
    flare.log("m1")           # ocupa a fila (1/2)
    flare.log("m2")           # ocupa a fila (2/2)
    accepted = flare.capture({"message": "m3"})  # fila cheia → descartado

    release.set()
    flare.close()

    assert accepted is False
    assert flare.dropped == 1


def test_on_error_that_raises_is_swallowed() -> None:
    def bad(_exc: Exception) -> None:
        raise RuntimeError("callback ruim")

    transport = FakeTransport(fail_with=TransientError("503"))
    flare = _client(transport, max_retries=0, on_error=bad)
    flare.log("x")
    # Se o callback ruim vazasse, o worker morreria e o flush penduraria.
    assert flare.flush(timeout=2) is True
    flare.close()


def test_stop_flushes_pending_batch(fake_transport: FakeTransport) -> None:
    # Sem flush explícito: o close precisa entregar o que ficou no batch.
    flare = _client(fake_transport, batch_size=100, flush_interval=10.0)
    flare.log("pendente")
    flare.close()
    assert any(e["message"] == "pendente" for e in fake_transport.sent_events)


def test_generic_transport_error_is_swallowed() -> None:
    # Um erro que não é Permanent nem Transient (bug inesperado do transporte) não
    # pode matar o worker: é engolido e reportado.
    errors: list[Exception] = []
    transport = FakeTransport(fail_with=ValueError("inesperado"))
    flare = _client(transport, on_error=errors.append)
    flare.log("x")
    flare.flush(timeout=2)
    flare.close()
    assert transport.calls == 1
    assert isinstance(errors[0], ValueError)


def test_retry_backoff_then_success() -> None:
    # Falha transiente uma vez, dorme o backoff, e a segunda tentativa passa.
    from flare_sdk._transport import TransientError

    transport = FakeTransport(fail_with=TransientError("503"), fail_times=1)
    flare = _client(transport, max_retries=3, retry_backoff=0.01)
    flare.log("x")
    flare.flush(timeout=2)
    flare.close()
    assert transport.calls == 2
    assert len(transport.sent_events) == 1


def test_report_without_callback_is_silent() -> None:
    from flare_sdk._transport import PermanentError

    # Sem on_error, uma falha não pode levantar nem pendurar o flush.
    transport = FakeTransport(fail_with=PermanentError("403"))
    flare = _client(transport)  # on_error=None
    flare.log("x")
    assert flare.flush(timeout=2) is True
    flare.close()


def test_flush_before_any_event_returns_true(fake_transport: FakeTransport) -> None:
    flare = _client(fake_transport)
    assert flare.flush(timeout=1) is True  # worker nunca nasceu
    flare.close()


def test_flush_after_close_returns_true(fake_transport: FakeTransport) -> None:
    flare = _client(fake_transport)
    flare.log("x")
    flare.close()
    assert flare.flush(timeout=1) is True


def test_worker_recreated_after_fork(
    fake_transport: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    flare = _client(fake_transport)
    flare.log("parent")
    flare.flush(timeout=2)
    first_worker = flare._worker

    # Simula o fork trocando o PID visto pelo cliente: o worker do "pai" deve ser
    # substituído por um novo, com fila nova, no "filho".
    monkeypatch.setattr("os.getpid", lambda: (flare._pid or 0) + 1)
    flare.log("child")
    flare.flush(timeout=2)
    flare.close()

    assert flare._worker is not first_worker
    assert any(e["message"] == "child" for e in fake_transport.sent_events)
