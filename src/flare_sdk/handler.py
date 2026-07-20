"""
FlareHandler — pluga o Flare no ``logging`` padrão com uma linha.
=================================================================

Depois de ``logging.getLogger().addHandler(FlareHandler(...))``, tudo o que a app
já loga passa a chegar ao Flare, sem trocar uma única chamada de ``logger.info``.
É a integração de menor atrito: o time continua usando o ``logging`` de sempre.

O mapeamento LogRecord → evento do Flare
----------------------------------------
* ``record.created`` → ``dt`` (epoch de emissão).
* ``record.getMessage()`` → ``message`` (já aplica os ``%s`` dos args).
* ``record.levelname`` → ``severity`` (DEBUG/INFO/WARNING/ERROR/CRITICAL: o Flare
  conhece todos esses nomes).
* ``record.name`` → atributo ``logger``; ``exc_info`` → atributo ``exception``.
* O que veio via ``extra={...}`` vira atributo — é assim que campos estruturados
  (``order_id``, ``user_id``) chegam ao Flare sem virar texto no meio da mensagem.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .client import Flare

#: Atributos que o ``logging`` põe em TODO record. Ficam de fora dos "extras" para
#: que só o que o usuário passou via ``extra={...}`` vire atributo do Flare — senão
#: cada evento carregaria ``pathname``, ``threadName`` e afins, ruído puro. O nome
#: ``message``/``asctime`` também entra: são derivados que o formatter cria.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)


class FlareHandler(logging.Handler):
    """Handler de ``logging`` que envia cada record ao Flare via :class:`Flare`.

    Dois modos de uso:

    * autônomo — ``FlareHandler(token=..., endpoint=...)`` cria e é dono do cliente
      (fecha-o no ``close`` do handler);
    * compartilhado — ``FlareHandler(client=meu_flare)`` reaproveita um cliente que
      você já usa para ``log``/``request`` manuais, para não abrir duas filas.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        client: Optional[Flare] = None,
        endpoint: Optional[str] = None,
        level: int = logging.NOTSET,
        **client_kwargs: Any,
    ) -> None:
        super().__init__(level=level)
        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = Flare(token, endpoint=endpoint, **client_kwargs)
            self._owns_client = True

    def emit(self, record: logging.LogRecord) -> None:
        """Converte o record e o enfileira. Erro aqui vira ``handleError``, nunca sobe.

        ``capture`` já é never-raise, mas a MONTAGEM do evento (formatar a mensagem,
        serializar um arg exótico) pode levantar — e o contrato de um handler é que
        ``emit`` jamais derrube quem está logando. Daí o ``handleError``.
        """
        try:
            self._client.capture(self._to_event(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def _to_event(self, record: logging.LogRecord) -> dict:
        """LogRecord → dict no contrato do ``/ingest``."""
        event: dict[str, Any] = {
            "dt": record.created,
            "message": record.getMessage(),
            "severity": record.levelname,
            "logger": record.name,
        }
        if record.exc_info:
            # A stack formatada é mais útil como um atributo pesquisável do que
            # concatenada na mensagem.
            event["exception"] = self.formatter.formatException(record.exc_info) \
                if self.formatter else logging.Formatter().formatException(record.exc_info)
        event.update(self._extras(record))
        return event

    @staticmethod
    def _extras(record: logging.LogRecord) -> dict:
        """Os campos passados via ``extra={...}`` — o que não é atributo padrão.

        Ignora chaves com ``_`` na frente (convenção de "privado" e onde o próprio
        ``logging`` guarda internos), para não vazar detalhe de implementação como
        atributo.
        """
        return {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_")
        }

    def close(self) -> None:
        """Fecha o cliente próprio (drena a fila) e some do registro do ``logging``.

        Só fecha o cliente se for dono dele: um cliente compartilhado tem outro dono
        e fechá-lo aqui mataria a fila que o resto da app ainda usa.
        """
        try:
            if self._owns_client:
                self._client.close()
        finally:
            super().close()
