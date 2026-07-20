"""Testes do FlareHandler: mapeamento de LogRecord e never-raise."""
from __future__ import annotations

import logging

import pytest

from flare_sdk.handler import FlareHandler
from tests.conftest import FakeClient


def _logger_with(handler: logging.Handler, name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def test_maps_level_message_logger() -> None:
    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.basic")
    logger.info("hello %s", "world")

    (event,) = client.events
    assert event["message"] == "hello world"
    assert event["severity"] == "INFO"
    assert event["logger"] == "t.basic"
    assert "dt" in event


def test_extra_fields_become_attributes() -> None:
    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.extra")
    logger.warning("charge failed", extra={"order_id": 42, "gateway": "pagarme"})

    (event,) = client.events
    assert event["severity"] == "WARNING"
    assert event["order_id"] == 42
    assert event["gateway"] == "pagarme"


def test_standard_record_attrs_do_not_leak() -> None:
    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.noleak")
    logger.info("x")
    event = client.events[0]
    # Ruído do logging não deve virar atributo.
    for noise in ("pathname", "threadName", "processName", "levelno"):
        assert noise not in event


def test_exception_is_captured() -> None:
    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.exc")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("deu ruim")

    event = client.events[0]
    assert event["severity"] == "ERROR"
    assert "ValueError: boom" in event["exception"]


def test_emit_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class ExplodingClient(FakeClient):
        def capture(self, event):  # noqa: ANN001, ANN201
            raise RuntimeError("capture explodiu")

    handler = FlareHandler(client=ExplodingClient())
    handled: list[bool] = []
    monkeypatch.setattr(handler, "handleError", lambda record: handled.append(True))
    logger = _logger_with(handler, "t.raise")

    logger.info("x")  # não pode propagar
    assert handled == [True]


def test_close_closes_owned_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # Handler dono do cliente: ao fechar, fecha o cliente também.
    created = FakeClient()
    monkeypatch.setattr("flare_sdk.handler.Flare", lambda *a, **k: created)
    handler = FlareHandler(token="tok", endpoint="https://flare.test/ingest")
    handler.close()
    assert created.closed is True


def test_close_does_not_close_shared_client() -> None:
    client = FakeClient()
    handler = FlareHandler(client=client)
    handler.close()
    assert client.closed is False
