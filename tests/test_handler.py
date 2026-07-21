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


def test_source_location_is_attached_as_context() -> None:
    """Todo log carrega a ORIGEM (file/func/line/module) — o "de onde saiu", que
    vira o Context na tela. Sem isso o dashboard só teria o nome do logger."""
    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.src")
    logger.info("oi")

    event = client.events[0]
    assert event["file"].endswith("test_handler.py")
    assert event["func"] == "test_source_location_is_attached_as_context"
    assert isinstance(event["line"], int) and event["line"] > 0
    assert event["module"] == "test_handler"


def test_a_user_extra_cannot_overwrite_the_real_source_location() -> None:
    """`extra={"file"/"func"/"line": ...}` é permitido pelo logging (não são nomes
    padrão), mas não pode SOBRESCREVER a origem real que o handler derivou — o dado
    de negócio do usuário não deve poder mentir sobre de onde o log saiu."""
    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.src.spoof")
    logger.info("oi", extra={"file": "MENTIRA.py", "func": "fake", "line": -1})

    event = client.events[0]
    assert event["file"].endswith("test_handler.py")  # a origem REAL, não a mentira
    assert event["func"] == "test_a_user_extra_cannot_overwrite_the_real_source_location"
    assert event["line"] > 0


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


def test_exception_is_captured_as_structured_object() -> None:
    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.exc")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("deu ruim")

    event = client.events[0]
    assert event["severity"] == "ERROR"
    exc = event["exception"]
    # Agora é um objeto estruturado, não mais a stack crua numa string.
    assert isinstance(exc, dict)
    assert exc["type"] == "ValueError"
    assert "boom" in exc["message"]
    assert "ValueError" in exc["traceback"]
    # ``where`` aponta o último frame: este arquivo e a linha do ``raise``.
    assert "test_handler.py:" in exc["where"]
    assert " in test_exception_is_captured_as_structured_object" in exc["where"]


def test_exception_object_never_raises_on_a_hostile_str() -> None:
    """Uma exceção cujo ``__str__`` explode não pode derrubar a montagem do evento.

    É o pior caso do never-raise: ``format_exception`` E o ``formatException`` de
    fallback chamam ``str(exc_value)`` e também estourariam; ``str(exc_value)`` no
    ``message`` idem. A montagem tem de sobreviver com fallbacks primitivos — senão
    o handler de ERRO seria o que derruba a app ao logar um erro.
    """
    import sys

    class Hostile(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str explodiu")

    try:
        raise Hostile()
    except Hostile:
        exc_info = sys.exc_info()

    obj = FlareHandler._exception_object(exc_info)  # não pode levantar

    assert isinstance(obj, dict)
    assert obj["type"] == "Hostile"
    # `str(exc_value)` direto estourou → o `message` caiu no fallback vazio. (O
    # `format_exception` do traceback NÃO estoura — ele tolera o __str__ ruim e põe
    # um placeholder —, então o traceback é uma string, só não vem vazio.)
    assert obj["message"] == ""
    assert isinstance(obj["traceback"], str)
    assert isinstance(obj["where"], str)


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
