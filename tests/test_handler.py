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


# ── O trace_id: o log sai amarrado à request da mesma chamada ────────────────


def test_a_log_inside_a_trace_carries_the_trace_id() -> None:
    """A METADE DE LOG DO ELO. O middleware abre o escopo por request; cada log
    emitido dentro dele sai com o mesmo id, sem que a chamada precise repetir nada.
    Sem isto, ligar erro e request dependeria de cada `logger.error` do código
    lembrar de passar o id — e bastaria um esquecer para o erro sair órfão."""
    from flare_sdk import reset_trace_id, set_trace_id

    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.trace")

    token = set_trace_id("abc123")
    try:
        logger.error("falhou")
    finally:
        reset_trace_id(token)

    assert client.events[0]["trace_id"] == "abc123"


def test_without_a_trace_the_field_is_absent() -> None:
    """Fora de uma request (script, worker, import) não há transação a
    correlacionar. O campo fica ausente — NULL na coluna — em vez de um id
    inventado que amarraria o log a coisa nenhuma."""
    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.notrace")
    logger.error("solto")

    assert "trace_id" not in client.events[0]


def test_an_explicit_trace_id_wins_over_the_context() -> None:
    """`extra={"trace_id": ...}` é a app dizendo "este log pertence ÀQUELA outra
    transação" — um retry, um job disparado por outra request. O contexto não pode
    sobrescrever essa afirmação."""
    from flare_sdk import reset_trace_id, set_trace_id

    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.trace.explicit")

    token = set_trace_id("do-contexto")
    try:
        logger.error("de outra transação", extra={"trace_id": "o-meu"})
    finally:
        reset_trace_id(token)

    assert client.events[0]["trace_id"] == "o-meu"


def test_an_explicit_empty_trace_id_suppresses_the_context() -> None:
    """`extra={"trace_id": ""}` é a app dizendo "este log NÃO pertence a transação
    nenhuma" — um job disparado de dentro de uma request, mas que é outra coisa.
    Testar truthiness em vez de PRESENÇA transformava essa supressão no oposto: o
    log saía amarrado justo à transação da qual se quis separá-lo."""
    from flare_sdk import reset_trace_id, set_trace_id

    client = FakeClient()
    logger = _logger_with(FlareHandler(client=client), "t.trace.suprimido")

    token = set_trace_id("do-contexto")
    try:
        logger.error("fora da transação", extra={"trace_id": ""})
    finally:
        reset_trace_id(token)

    assert client.events[0]["trace_id"] == ""
