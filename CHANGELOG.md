# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
o projeto segue [SemVer](https://semver.org/lang/pt-BR/).

## [0.6.0]

### Adicionado

- **`trace_id`: o elo entre um log e a request que o produziu.** O Flare guarda
  `trace_id` como coluna nas duas tabelas e usa essa coluna para, no dashboard,
  sair de um erro e abrir a request que o causou (e a volta). O SDK nunca mandava
  o campo, então a coluna ficava `NULL` em todo evento — os dois sinais existiam
  lado a lado e não se tocavam, e a funcionalidade estava desligada em silêncio.

  Agora o `FlareMiddleware` abre um escopo de trace por request (um `ContextVar`) e:

  - grava o id na linha da **request**;
  - todo **log** emitido durante a chamada sai com o mesmo id, sem que o código
    precise repeti-lo — passar por parâmetro exigiria enfiar o id em toda
    assinatura entre o middleware e o `logger.error`, e bastaria uma função
    esquecer para aquele erro sair órfão.

  A app LÊ o id com `get_trace_id()` para usá-lo nas próprias mensagens. A direção
  importa: o middleware escreve, a app lê. Um `set_trace_id` dentro de um endpoint
  declarado `def` (não `async def`) roda no threadpool, com uma cópia do contexto,
  e **não** volta para o middleware — a request seria gravada sem o id que os logs
  usaram, e o elo quebraria justo nos endpoints síncronos.

  Um id já presente no contexto vence (um `X-Request-Id` do gateway, o id de uma
  mensagem de fila): quem o pôs está afirmando de que transação a chamada faz
  parte, e gerar outro por cima partiria o rastro em dois. Quem já gerencia o
  próprio contexto (OpenTelemetry) desliga com `FlareMiddleware(trace=False)`.

- `set_trace_id` / `get_trace_id` / `reset_trace_id` / `new_trace_id` no topo do
  pacote.

## [0.5.0]

### Adicionado

- `FlareMiddleware`: opção `flush_after_request` (com `flush_timeout`), para
  serverless. Em Lambda/Cloud Run a thread de entrega congela com o container, e o
  reflexo — um `@app.middleware("http")` chamando `flare.flush()` depois do
  `call_next` — **drena a fila cedo demais**: o `call_next` do `BaseHTTPMiddleware`
  retorna no `http.response.start`, antes de o corpo ser transmitido e antes de o
  middleware gravar a request. O evento da request atual ficava para trás e só saía
  numa invocação seguinte, se houvesse — na prática, a request mais recente nunca
  aparecia no dashboard. Com a opção, o flush roda dentro do middleware logo após o
  registro, e não há como inverter a ordem.

## [0.4.0]

### Adicionado

- `FlareHandler`: cada log passa a carregar a ORIGEM como atributos — `file`
  (`pathname`), `func` (`funcName`), `line` (`lineno`) e `module`. É o "de onde o
  log saiu", que a tela de detalhe do Flare mostra no Context. Antes só ia o
  `logger` (o nome), e achar a linha que emitiu a mensagem virava caça no código.

## [0.3.0]

### Adicionado

- `FlareHandler`: a exceção (`exc_info`) agora vira um objeto estruturado no
  atributo `exception`, com `type` (ex.: `"ValueError"`), `message`, `traceback`
  (a stack completa como string) e `where` — o formato LITERAL
  `` `<arquivo>:<linha> in <função>` `` do último frame (o `in` é literal, no padrão
  de traceback do Python; ex.: `main.py:158 in send_facebook_pixel`). Antes a
  exceção era um blob de texto com a stack crua — ilegível e não consultável.
  Agora o front do Flare mostra tipo/mensagem/onde separados e filtráveis. A
  extração é protegida (never-raise): se falhar, cai num `where=""` e no traceback
  via `formatException`.
- `FlareMiddleware`: captura opt-in dos headers da request e da response
  (`capture_request_headers` / `capture_response_headers`), enviados como os
  atributos `request_headers` / `response_headers` — cada atributo é um dict `{nome: valor}`
  (nomes em minúsculas, valores decodificados latin-1). Headers sensíveis
  (`authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `api-key`,
  `x-api-key`, `x-auth-token`) têm o VALOR redigido para `"***"` — o nome permanece,
  o segredo não vaza para o dashboard. OFF por padrão.

## [0.2.0]

### Adicionado

- `FlareMiddleware`: captura opt-in do corpo da request e da response
  (`capture_request_body` / `capture_response_body`), enviados como os atributos
  `request_body` / `response_body` — as chaves que a tela de detalhe do Flare mostra
  nas abas Request/Response. Corpo capado em `max_body_bytes` (16 KB por padrão) e
  só capturado quando o `Content-Type` é textual (JSON/texto/form); binário é
  ignorado. ⚠️ Pode conter PII/tokens — por isso é OFF por padrão.

## [0.1.0]

### Adicionado

- Cliente `Flare`: fila em background, envio em lote, retry de erro transiente e a
  garantia de nunca derrubar a app hospedeira (never-raise).
- `FlareHandler`: integração de uma linha com o `logging` padrão; `extra={...}`
  vira atributo no Flare.
- `FlareMiddleware` (ASGI): instrumenta requests de FastAPI/Starlette com
  `method`/`path`/`status_code`/`duration_ms`, agrupando por template de rota.
- Transporte sem dependências (stdlib `urllib`); FastAPI como extra opcional.
- Resiliência a `fork` (uvicorn/gunicorn): a thread de entrega é recriada por
  processo.
