"""Testes do transporte: serialização NDJSON e classificação de erro HTTP."""
from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from flare_sdk._transport import (
    PermanentError,
    Transport,
    TransientError,
    encode_ndjson,
)


def test_encode_ndjson_one_line_per_event() -> None:
    body = encode_ndjson([{"a": 1}, {"b": 2}])
    lines = body.decode("utf-8").split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"b": 2}


def test_encode_ndjson_serializes_exotic_types_via_str() -> None:
    # Um datetime não é JSON-serializável de fábrica; o default=str o salva em vez
    # de derrubar o lote inteiro.
    moment = datetime(2026, 7, 20, tzinfo=timezone.utc)
    body = encode_ndjson([{"dt": moment}])
    assert "2026-07-20" in body.decode("utf-8")


def _fake_urlopen(status_body: bytes):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return status_body

    def _open(request, timeout=None):  # noqa: ANN001
        return _Resp()

    return _open


def test_send_returns_accepted_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen", _fake_urlopen(b'{"accepted": 3}')
    )
    transport = Transport("https://flare.test/ingest", "tok")
    assert transport.send([{"a": 1}, {"a": 2}, {"a": 3}]) == 3


def test_send_bad_response_body_counts_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen(b"not json"))
    transport = Transport("https://flare.test/ingest", "tok")
    assert transport.send([{"a": 1}]) == 0


@pytest.mark.parametrize("code", [400, 401, 403, 406, 413, 422])
def test_4xx_is_permanent(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    def _raise(request, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            "https://flare.test/ingest", code, "boom", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    transport = Transport("https://flare.test/ingest", "tok")
    with pytest.raises(PermanentError):
        transport.send([{"a": 1}])


@pytest.mark.parametrize("code", [429, 500, 502, 503])
def test_5xx_and_429_are_transient(
    monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def _raise(request, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            "https://flare.test/ingest", code, "boom", {}, io.BytesIO(b"")
        )

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    transport = Transport("https://flare.test/ingest", "tok")
    with pytest.raises(TransientError):
        transport.send([{"a": 1}])


def test_network_error_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(request, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    transport = Transport("https://flare.test/ingest", "tok")
    with pytest.raises(TransientError):
        transport.send([{"a": 1}])


def test_timeout_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(request, timeout=None):  # noqa: ANN001
        raise TimeoutError("timed out")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    transport = Transport("https://flare.test/ingest", "tok")
    with pytest.raises(TransientError):
        transport.send([{"a": 1}])
