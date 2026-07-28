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
* Origem do log → atributos ``file`` (``pathname``), ``func`` (``funcName``),
  ``line`` (``lineno``) e ``module`` — o "de onde saiu", que a tela mostra no
  Context. Sem isso, achar a linha que emitiu a mensagem seria caça no código.
* O que veio via ``extra={...}`` vira atributo — é assim que campos estruturados
  (``order_id``, ``user_id``) chegam ao Flare sem virar texto no meio da mensagem.

A exceção vai ESTRUTURADA, não como blob de texto
--------------------------------------------------
``exc_info`` vira um objeto ``{type, message, traceback, where}``, não a stack crua
numa string. O porquê: o front do Flare mostra tipo, mensagem e "onde estourou"
como campos separados (filtráveis, agrupáveis) — um blob de texto sozinho é
ilegível e não dá para consultar por ``type == "ValueError"``. O ``traceback``
completo continua lá (como string) para quem precisa da stack inteira; o ``where``
é o último frame (arquivo:linha in função), o ponto exato da falha.
"""
from __future__ import annotations

import logging
import os
import traceback
from typing import Any, Optional

from ._trace import get_trace_id
from .client import Flare

#: Atributos que o ``logging`` põe em TODO record. Ficam de fora dos "extras" para
#: que só o que o usuário passou via ``extra={...}`` vire atributo do Flare — senão
#: cada evento carregaria ``pathname``, ``threadName`` e afins, ruído puro. O nome
#: ``message``/``asctime`` também entra: são derivados que o formatter cria.
#:
#: ``file``/``func``/``line`` NÃO são nomes de atributo padrão do LogRecord (os
#: padrão são ``pathname``/``funcName``/``lineno``), então o ``logging`` deixa um
#: ``extra={"file": ...}`` passar. Reservá-los aqui impede que esse extra
#: sobrescreva a ORIGEM real que ``_to_event`` derivou — o valor de negócio do
#: usuário não deve poder mentir sobre de onde o log saiu. (``module`` já é padrão.)
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
        "file", "func", "line",
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
        """LogRecord → dict no contrato do ``/ingest``.

        Anexa a ORIGEM do log (``file``/``func``/``line``/``module``) além do
        ``logger``: é a metade útil do contexto — "de onde este log saiu". Sem ela,
        o dashboard mostraria só o nome do logger, e achar a linha que emitiu a
        mensagem viraria caça no código. São atributos (viram Context na tela).
        """
        event: dict[str, Any] = {
            "dt": record.created,
            "message": record.getMessage(),
            "severity": record.levelname,
            "logger": record.name,
            "file": record.pathname,
            "func": record.funcName,
            "line": record.lineno,
            "module": record.module,
        }
        if record.exc_info:
            event["exception"] = self._exception_object(record.exc_info)
        event.update(self._extras(record))
        # O `trace_id` da transação em curso, quando há uma. Entra DEPOIS dos
        # extras e só se ninguém já o definiu: um `extra={"trace_id": ...}`
        # explícito é a app dizendo "este log pertence àquela outra transação"
        # (um retry, um job disparado por outra request), e o contexto não pode
        # sobrescrever essa afirmação.
        #
        # É o que amarra este log à linha de `flare_requests` da mesma chamada —
        # sem isso, cada `logger.error` teria de repetir o id à mão, e bastaria um
        # esquecer para o erro sair órfão justo quando alguém for investigá-lo.
        if not event.get("trace_id"):
            trace_id = get_trace_id()
            if trace_id:
                event["trace_id"] = trace_id
        return event

    @staticmethod
    def _exception_object(exc_info: Any) -> dict:
        """``exc_info`` → objeto estruturado ``{type, message, traceback, where}``.

        Estruturado em vez de blob porque o front mostra tipo/mensagem/onde
        separados (e o operador filtra por eles); a string sozinha não é
        consultável. ``where`` é o ÚLTIMO frame do traceback (arquivo:linha in
        função) — o ponto onde de fato estourou.

        Never-raise: extrair frame/tipo pode falhar em cenários exóticos (traceback
        truncado, ``__name__`` ausente). Se falhar, cai num ``where=""`` e no
        traceback via ``formatException`` — nunca deixa a montagem do evento subir.
        """
        exc_type, exc_value, exc_tb = exc_info
        # A stack, com fallback DO fallback: `formatException` também pode levantar,
        # e aí a string vazia é o último recurso — a montagem nunca sobe.
        try:
            stack = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        except Exception:  # noqa: BLE001
            try:
                stack = logging.Formatter().formatException(exc_info)
            except Exception:  # noqa: BLE001
                stack = ""
        where = ""
        try:
            frames = traceback.extract_tb(exc_tb)
            if frames:
                last = frames[-1]
                where = f"{os.path.basename(last.filename)}:{last.lineno} in {last.name}"
        except Exception:  # noqa: BLE001
            where = ""
        # `type`/`message` também sob proteção: um exc_value com `__str__` (ou
        # `__bool__`) que levanta não pode derrubar o handler de erro. Checa `None`
        # (não truthiness — um `__bool__` exótico), converte protegido, cai em "".
        try:
            type_name = exc_type.__name__ if exc_type is not None else ""
        except Exception:  # noqa: BLE001
            type_name = ""
        message = ""
        if exc_value is not None:
            try:
                message = str(exc_value)
            except Exception:  # noqa: BLE001
                message = ""
        return {
            "type": type_name,
            "message": message,
            "traceback": stack,
            "where": where,
        }

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
