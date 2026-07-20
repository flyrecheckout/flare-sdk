"""
Transporte HTTP — o POST cru para o ``/ingest``, só com a stdlib.
==================================================================

Uma única responsabilidade: pegar uma lista de eventos, serializar em NDJSON e
mandar um POST. Nada de fila, nada de thread — isso é do :mod:`flare_sdk.client`.
Manter o transporte burro é o que o torna testável sem rede (o teste troca a função
de envio) e sem depender de httpx/requests.

Classificação de erro — o ponto que decide o retry
--------------------------------------------------
O worker do cliente precisa saber se vale a pena tentar de novo. Por isso o erro
sai tipado:

* :class:`PermanentError` — o servidor recusou o **conteúdo** (403 token inválido,
  406 lote malformado, 413 grande demais). Tentar de novo manda o mesmo corpo e
  recebe o mesmo 4xx: é jogar fora com aviso, não repetir.
* :class:`TransientError` — o servidor ou a rede falharam **agora** (5xx, 429,
  timeout, conexão recusada). O mesmo corpo pode passar daqui a instantes: retenta.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping, Sequence

from ._version import __version__

#: Content-Type do lote. NDJSON (uma linha por evento) é o formato natural de um
#: batch e é um dos três contratos que o ``/ingest`` aceita.
_CONTENT_TYPE = "application/x-ndjson"

#: 4xx que são culpa do conteúdo, não do momento — não adianta retentar.
_PERMANENT_STATUS = frozenset({400, 401, 403, 404, 406, 413, 422})


class FlareTransportError(Exception):
    """Base de qualquer falha de envio. O cliente nunca a deixa subir para a app."""


class PermanentError(FlareTransportError):
    """O servidor recusou o conteúdo (4xx). Retentar repetiria o mesmo erro."""


class TransientError(FlareTransportError):
    """Falha momentânea (5xx, 429, rede). Vale retentar com backoff."""


def encode_ndjson(events: Sequence[Mapping[str, Any]]) -> bytes:
    """Serializa os eventos em NDJSON: um JSON por linha, UTF-8.

    ``default=str`` é a rede de segurança contra o que não é JSON-serializável de
    fábrica (um ``datetime``, um ``UUID``, um ``Decimal`` que caiu nos atributos):
    vira string em vez de estourar o worker inteiro e derrubar o lote.
    """
    lines = [json.dumps(event, ensure_ascii=False, default=str) for event in events]
    return ("\n".join(lines)).encode("utf-8")


class Transport:
    """Faz um POST por chamada de :meth:`send`. Sem estado além da config.

    Guardar só ``endpoint``/``token``/``timeout`` (imutáveis) torna a instância
    segura para ser compartilhada pela thread do worker sem lock.
    """

    def __init__(self, endpoint: str, token: str, *, timeout: float = 5.0) -> None:
        self._endpoint = endpoint
        self._token = token
        self._timeout = timeout
        self._user_agent = f"flare-sdk/{__version__}"

    def send(self, events: Sequence[Mapping[str, Any]]) -> int:
        """Manda o lote e devolve quantas linhas o servidor aceitou.

        Levanta :class:`PermanentError` ou :class:`TransientError` conforme a
        classificação — nunca uma exceção crua de ``urllib`` (o worker decide o
        retry pelo tipo, não pelo código HTTP espalhado por lá).
        """
        body = encode_ndjson(events)
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": _CONTENT_TYPE,
                "User-Agent": self._user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return _accepted_count(response.read())
        except urllib.error.HTTPError as exc:
            # HTTPError carrega o código: 4xx conhecido é permanente, o resto
            # (5xx, 429) é transiente.
            if exc.code in _PERMANENT_STATUS:
                raise PermanentError(f"{exc.code} {exc.reason}") from exc
            raise TransientError(f"{exc.code} {exc.reason}") from exc
        except urllib.error.URLError as exc:
            # Sem código HTTP: DNS, conexão recusada, timeout — tudo momentâneo.
            raise TransientError(str(exc.reason)) from exc
        except (TimeoutError, OSError) as exc:
            # Timeout do socket / erro de I/O de baixo nível também é transiente.
            raise TransientError(str(exc)) from exc


def _accepted_count(raw: bytes) -> int:
    """Lê ``{"accepted": N}`` da resposta 202; qualquer desvio conta como 0.

    A contagem é informativa (métrica de quantas linhas entraram). Uma resposta
    sem corpo ou fora do formato não é erro de envio — o POST já foi aceito —,
    então cai em 0 em vez de levantar.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 0
    if isinstance(payload, dict) and isinstance(payload.get("accepted"), int):
        return payload["accepted"]
    return 0
