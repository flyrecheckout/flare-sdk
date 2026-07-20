"""
O cliente Flare — fila, worker em background, batch e a promessa de nunca cair.
================================================================================

Este é o coração do SDK. A app chama :meth:`Flare.log`, :meth:`Flare.request` ou
:meth:`Flare.capture` e volta na mesma hora: o evento entra numa fila e uma thread
de background o entrega. Três invariantes guiam o desenho:

1. **Telemetria nunca derruba a app.** Toda falha de envio é engolida (e reportada
   pelo ``on_error``, se houver). Um Flare fora do ar não pode virar um 500 na API
   do cliente. Por isso ``log``/``request``/``capture`` jamais levantam.
2. **Nunca bloquear o caminho da request.** A fila é não-bloqueante: se ela encher
   (Flare lento ou fora do ar), o evento é **descartado** e contado — nunca se
   segura a thread da app esperando espaço. Perder telemetria é aceitável; travar
   a API não é.
3. **Sobreviver ao fork.** uvicorn/gunicorn criam o processo e depois forkam os
   workers; uma thread não cruza o ``fork``. O worker é (re)criado por processo,
   detectando a troca de PID — senão o worker do pai ficaria morto no filho e nada
   seria enviado.
"""
from __future__ import annotations

import atexit
import os
import queue
import threading
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from ._transport import PermanentError, TransientError, Transport

#: Sentinela de parada: enfileirada por :meth:`Flare.close` para o worker drenar o
#: que resta e sair. Objeto único (identidade) para nunca colidir com um evento.
_STOP = object()


class _Flush:
    """Pedido de flush: o worker esvazia o batch atual e sinaliza o ``event``.

    Carrega o próprio ``Event`` para que :meth:`Flare.flush` saiba, sem polling,
    o instante exato em que o batch pendente terminou de ser enviado.
    """

    __slots__ = ("event",)

    def __init__(self) -> None:
        self.event = threading.Event()


class Flare:
    """Cliente de telemetria: enfileira eventos e os entrega em lote, em background.

    Uso típico::

        flare = Flare(token="...", endpoint="https://flare.example.com/ingest")
        flare.log("pagamento aprovado", severity="INFO", order_id=42)
        flare.request("POST", "/charge", 201, duration_ms=87.4)

    ``token`` e ``endpoint`` caem para as variáveis de ambiente ``FLARE_TOKEN`` e
    ``FLARE_INGEST_URL`` quando omitidos — é o que permite ligar o SDK sem tocar no
    código, só com config de ambiente.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        endpoint: Optional[str] = None,
        batch_size: int = 100,
        flush_interval: float = 2.0,
        max_queue: int = 10_000,
        timeout: float = 5.0,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
        default_attributes: Optional[Mapping[str, Any]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        token = token or os.environ.get("FLARE_TOKEN")
        endpoint = endpoint or os.environ.get("FLARE_INGEST_URL")
        # Config faltando é erro de boot, e AQUI pode levantar: falhar no start é o
        # oposto de falhar no meio de uma request. O never-raise vale para o envio,
        # não para construir um cliente sem para onde enviar.
        if not token:
            raise ValueError(
                "Flare token ausente: passe token=... ou defina FLARE_TOKEN."
            )
        if not endpoint:
            raise ValueError(
                "Flare endpoint ausente: passe endpoint=... ou defina FLARE_INGEST_URL."
            )

        self._transport = Transport(endpoint, token, timeout=timeout)
        self._batch_size = max(1, batch_size)
        self._flush_interval = max(0.05, flush_interval)
        self._max_queue = max(1, max_queue)
        self._max_retries = max(0, max_retries)
        self._retry_backoff = max(0.0, retry_backoff)
        self._default_attributes = dict(default_attributes or {})
        self._on_error = on_error

        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self._max_queue)
        self._worker: Optional[threading.Thread] = None
        self._pid: Optional[int] = None
        self._lock = threading.Lock()
        self._closed = False
        self._dropped = 0

        # Fecha no fim do processo para não perder o que sobrou na fila. É registrado
        # uma vez; ``close`` é idempotente, então um atexit + um close manual não
        # brigam.
        atexit.register(self.close)

    # ── API pública ──────────────────────────────────────────────────────────

    @property
    def dropped(self) -> int:
        """Quantos eventos foram descartados por fila cheia. Sinal de saturação."""
        return self._dropped

    def log(
        self,
        message: str,
        *,
        severity: str = "INFO",
        dt: Optional[float] = None,
        **attributes: Any,
    ) -> bool:
        """Enfileira um log. ``severity`` é o nome OTel/logging (INFO, ERROR, ...).

        Campos extras viram atributos no Flare. ``dt`` (epoch em segundos) carimba
        o instante de emissão; omitido, usa agora — melhor que a hora de recepção
        do servidor, que embaralharia a ordem sob atraso de rede.
        """
        event = {
            **self._default_attributes,
            "dt": time.time() if dt is None else dt,
            "message": message,
            "severity": severity,
            **attributes,
        }
        return self.capture(event)

    def request(
        self,
        method: str,
        path: str,
        status_code: int,
        *,
        duration_ms: Optional[float] = None,
        dt: Optional[float] = None,
        **attributes: Any,
    ) -> bool:
        """Enfileira uma request (``_kind=request``): vira linha na tabela de requests.

        ``method``/``path``/``status_code``/``duration_ms`` são as colunas promovidas
        que o Flare espera; o resto (corpos, headers, ids) vira atributo.
        """
        event = {
            **self._default_attributes,
            "_kind": "request",
            "dt": time.time() if dt is None else dt,
            "method": method,
            "path": path,
            "status_code": status_code,
            "duration_ms": duration_ms,
            **attributes,
        }
        return self.capture(event)

    def capture(self, event: Mapping[str, Any]) -> bool:
        """Enfileira um evento cru (escape hatch para mandar qualquer coisa).

        Devolve ``True`` se entrou na fila, ``False`` se foi descartado (fila cheia
        ou cliente fechado). Nunca bloqueia e nunca levanta — é o ponto onde o
        never-raise é garantido para todos os outros métodos, que passam por aqui.
        """
        if self._closed:
            return False
        try:
            self._ensure_worker()
            self._queue.put_nowait(dict(event))
            return True
        except queue.Full:
            self._dropped += 1
            return False
        except Exception as exc:  # noqa: BLE001
            # Nada no enfileiramento pode escapar para a app. Um erro aqui é bug
            # nosso, reportado pelo canal de erro, jamais propagado.
            self._report(exc)
            return False

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Bloqueia até o que já está na fila ser enviado (ou ``timeout`` estourar).

        Devolve ``True`` se drenou a tempo. Útil em jobs curtos e testes, onde o
        processo terminaria antes de o worker entregar o último lote.
        """
        if self._closed or self._worker is None:
            return True
        marker = _Flush()
        try:
            self._queue.put_nowait(marker)
        except queue.Full:
            return False
        return marker.event.wait(timeout)

    def close(self) -> None:
        """Drena a fila, para o worker e libera o recurso. Idempotente.

        Chamado pelo ``atexit`` e seguro de chamar à mão. Depois de fechado, novos
        eventos são descartados — um cliente fechado não ressuscita.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker
        if worker is not None and worker.is_alive():
            self._queue.put(_STOP)
            # Teto de espera generoso mas finito: o pior caso é um último lote em
            # retry. Nunca se pendura o shutdown do processo indefinidamente.
            worker.join(timeout=self._flush_interval + self._timeout_budget())

    def __enter__(self) -> "Flare":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── Interno ─────────────────────────────────────────────────────────────

    def _timeout_budget(self) -> float:
        """Teto de tempo que o join tolera: soma dos backoffs de um último retry."""
        return self._retry_backoff * (2 ** self._max_retries) + 5.0

    def _ensure_worker(self) -> None:
        """Garante um worker vivo NESTE processo, recriando-o após um fork.

        Compara o PID guardado com o atual: se o processo forkou, a thread do pai
        não veio junto e a fila herdada pode estar num estado inconsistente — então
        troca-se por uma fila nova e um worker novo. O lock serializa a checagem
        para dois primeiros logs concorrentes não criarem dois workers.
        """
        pid = os.getpid()
        if self._worker is not None and self._pid == pid and self._worker.is_alive():
            return
        with self._lock:
            if self._worker is not None and self._pid == pid and self._worker.is_alive():
                return
            if self._pid != pid:
                # Fork detectado: a fila do pai não vale no filho.
                self._queue = queue.Queue(maxsize=self._max_queue)
            self._pid = pid
            self._worker = threading.Thread(
                target=self._run, name="flare-sdk-worker", daemon=True
            )
            self._worker.start()

    def _run(self) -> None:
        """Loop do worker: acumula um batch e o envia por tamanho ou por tempo.

        Bloqueia em ``get`` por até ``flush_interval``; o timeout é o gatilho de
        "manda o que tem mesmo sem encher o batch", para um evento solitário não
        ficar preso até chegar o centésimo.
        """
        batch: list[dict] = []
        while True:
            try:
                item = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                if batch:
                    self._send_with_retry(batch)
                    batch = []
                continue

            if item is _STOP:
                if batch:
                    self._send_with_retry(batch)
                return
            if isinstance(item, _Flush):
                if batch:
                    self._send_with_retry(batch)
                    batch = []
                item.event.set()
                continue

            batch.append(item)
            if len(batch) >= self._batch_size:
                self._send_with_retry(batch)
                batch = []

    def _send_with_retry(self, batch: Sequence[dict]) -> None:
        """Envia um lote, retentando só o que é transiente. Nunca levanta.

        Erro permanente (4xx) é descartado com aviso — retentar repetiria o 4xx.
        Erro transiente (5xx/rede) é retentado com backoff exponencial até
        ``max_retries``; esgotado, o lote é descartado e reportado. O batch inteiro
        cai junto porque o ``/ingest`` é atômico: não há "metade gravada".
        """
        delay = self._retry_backoff
        for attempt in range(self._max_retries + 1):
            try:
                self._transport.send(batch)
                return
            except PermanentError as exc:
                self._report(exc)
                return
            except TransientError as exc:
                if attempt >= self._max_retries:
                    self._report(exc)
                    return
                if delay > 0:
                    time.sleep(delay)
                delay *= 2
            except Exception as exc:  # noqa: BLE001
                # Qualquer coisa inesperada do transporte também é engolida: o
                # worker não pode morrer e deixar a fila sem quem a drene.
                self._report(exc)
                return

    def _report(self, exc: Exception) -> None:
        """Entrega o erro ao callback do usuário, blindado contra callback ruim.

        Um ``on_error`` que ele mesmo levante não pode derrubar o worker — seria a
        telemetria quebrando a telemetria. Sem callback, o erro é silencioso por
        desenho: um SDK que imprime no stderr a cada falha polui o log da app.
        """
        if self._on_error is None:
            return
        try:
            self._on_error(exc)
        except Exception:  # noqa: BLE001
            pass
