# flare-sdk

Cliente leve para enviar logs e requests ao [Flare](https://github.com/flyrecheckout/Flare),
o sistema de observabilidade da lunacheckout.

Uma app instrumentada manda telemetria para `POST /ingest` com o token da sua
source; o Flare recebe, resolve `token → source` e grava. Este pacote é o lado
_cliente_ desse contrato — a parte que **envia**.

```
[sua API]  ──flare-sdk──>  POST /ingest (Bearer token)  ──>  [Flare]  ──>  dashboard
```

## Princípios

- **Nunca derruba a sua app.** Toda falha de envio (Flare fora do ar, rede caída,
  token errado) é engolida. Telemetria quebrada não pode virar um 500 na sua API.
- **Nunca bloqueia a request.** Os eventos entram numa fila e uma thread de
  background os entrega em lote. Se a fila enche, o evento é descartado — nunca se
  segura o request esperando o Flare.
- **Zero dependências no core.** O transporte usa a stdlib (`urllib`). `pip install
  flare-sdk` não puxa mais nada. O middleware de FastAPI é um extra opcional que
  nem sequer importa framework novo (fala ASGI direto).
- **Sobrevive ao fork.** uvicorn/gunicorn forkam workers; o SDK recria a thread de
  entrega por processo, sem você pensar nisso.

## Instalação

```bash
pip install flare-sdk
# com o middleware de FastAPI (opcional; não puxa dependência nova):
pip install "flare-sdk[fastapi]"
```

## Começo rápido

O jeito mais comum: pluge no `logging` que você já usa. Uma linha, e tudo que a
app já loga passa a chegar ao Flare.

```python
import logging
from flare_sdk import FlareHandler

logging.getLogger().addHandler(
    FlareHandler(
        token="seu-source-token",
        endpoint="https://flare.lunacheckout.com/ingest",
    )
)

logging.getLogger("checkout").info(
    "pagamento aprovado", extra={"order_id": 42, "gateway": "pagarme"}
)
```

`order_id` e `gateway` viram **atributos** pesquisáveis no Flare — não texto
espremido na mensagem.

### Configuração por ambiente

`token` e `endpoint` caem para as variáveis `FLARE_TOKEN` e `FLARE_INGEST_URL`
quando omitidos. Assim você liga o SDK sem tocar no código:

```bash
export FLARE_TOKEN="seu-source-token"
export FLARE_INGEST_URL="https://flare.lunacheckout.com/ingest"
```

```python
from flare_sdk import FlareHandler
logging.getLogger().addHandler(FlareHandler())  # lê do ambiente
```

## Instrumentando requests (FastAPI / Starlette)

Uma linha registra `method`, `path`, `status_code` e `duration_ms` de cada request.
As rotas são agrupadas pelo _template_ (`/orders/{id}`), não pelo id concreto —
senão a tela de métricas explodiria em cardinalidade.

```python
from fastapi import FastAPI
from flare_sdk import Flare
from flare_sdk.fastapi import FlareMiddleware

app = FastAPI()
flare = Flare()  # token/endpoint do ambiente

app.add_middleware(FlareMiddleware, client=flare)
```

Se a rota levantar, a request é registrada como `500` e a exceção é **re-levantada**
— o middleware observa, não sequestra o seu erro.

### Em Lambda / Cloud Run: `flush_after_request=True`

Em serverless a thread de entrega **congela junto com o container** ao fim da
invocação, então o lote precisa sair antes disso:

```python
app.add_middleware(FlareMiddleware, client=flare, flush_after_request=True)
```

⚠️ **Não** resolva isso com um middleware próprio:

```python
@app.middleware("http")            # não faz o que parece
async def flush(request, call_next):
    response = await call_next(request)
    flare.flush(timeout=3)         # roda cedo demais
    return response
```

`@app.middleware("http")` é um `BaseHTTPMiddleware`, e o `call_next` dele retorna
no `http.response.start` — **antes** de o corpo ser transmitido e, portanto, antes
de o `FlareMiddleware` gravar a request. O flush acha a fila sem o evento atual;
ele fica para trás e só sai numa invocação seguinte, se houver. O sintoma é
traiçoeiro: **a request mais recente nunca aparece no dashboard**.

Com a opção, o flush roda dentro do próprio middleware, logo depois do registro —
não há como inverter a ordem. É bloqueante de propósito (segura a resposta alguns
ms para o lote sair), por isso é opt-in: num servidor de longa duração deixe `False`
e use o envio em background.

## Enviando eventos à mão

Além do handler, você pode mandar logs, requests ou qualquer evento direto:

```python
from flare_sdk import Flare

flare = Flare(token="...", endpoint="https://flare.lunacheckout.com/ingest")

flare.log("job iniciado", severity="INFO", job="reconciliação")
flare.request("POST", "/charge", 201, duration_ms=87.4, gateway="pagarme")
flare.capture({"message": "evento cru", "severity": "DEBUG", "qualquer": "coisa"})
```

Um mesmo cliente serve o handler e as chamadas manuais — passe-o ao handler para
compartilhar uma fila só:

```python
handler = FlareHandler(client=flare)
```

## Severidade

Use os nomes de sempre do `logging` (o Flare os entende todos): `DEBUG`, `INFO`,
`WARNING`, `ERROR`, `CRITICAL`. No modelo OTel do Flare, **erro** é tudo com número
`>= 17` (ERROR e CRITICAL/FATAL).

## Referência de configuração

| Parâmetro            | Default                | O que faz                                             |
| -------------------- | ---------------------- | ----------------------------------------------------- |
| `token`              | `$FLARE_TOKEN`         | Token da source (Bearer). Obrigatório.                |
| `endpoint`           | `$FLARE_INGEST_URL`    | URL completa do `/ingest`. Obrigatório.               |
| `batch_size`         | `100`                  | Máximo de eventos por POST.                           |
| `flush_interval`     | `2.0`                  | Segundos até mandar um lote parcial.                  |
| `max_queue`          | `10000`                | Teto da fila; além disso, descarta (ver `dropped`).   |
| `timeout`            | `5.0`                  | Timeout de cada POST, em segundos.                    |
| `max_retries`        | `3`                    | Retries de erro transiente (5xx/rede), com backoff.   |
| `default_attributes` | `{}`                   | Atributos anexados a todo evento (ex.: `service`).    |
| `on_error`           | `None`                 | Callback `(exc) -> None` para observar falhas de envio. |

Erros permanentes (4xx: token inválido, lote malformado, corpo grande demais) são
descartados **sem** retry — repetir mandaria o mesmo 4xx.

## Encerramento limpo

O cliente registra um `atexit` que drena a fila no fim do processo. Em jobs curtos,
force o envio com `flush()` ou use o context manager:

```python
with Flare() as flare:
    flare.log("job de 1 tiro")
# sai daqui com a fila drenada

flare.flush(timeout=5)  # ou explicitamente
```

## Observando a saúde do próprio SDK

```python
flare = Flare(..., on_error=lambda exc: logging.getLogger("flare").warning("envio falhou: %s", exc))
...
flare.dropped  # quantos eventos foram descartados por fila cheia (sinal de saturação)
```

## Desenvolvimento

```bash
pip install -e ".[dev]"
pytest
```

Nenhum teste toca a rede — o transporte é substituído por um dublê. Cobertura
mínima 85%.

## Licença

MIT.
