# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
o projeto segue [SemVer](https://semver.org/lang/pt-BR/).

## [0.3.0]

### Adicionado

- `FlareHandler`: a exceção (`exc_info`) agora vira um objeto estruturado no
  atributo `exception`, com `type` (ex.: `"ValueError"`), `message`, `traceback`
  (a stack completa como string) e `where` (`arquivo.py:linha in funcao` do último
  frame). Antes era um blob de texto com a stack crua — ilegível e não consultável.
  Agora o front do Flare mostra tipo/mensagem/onde separados e filtráveis. A
  extração é protegida (never-raise): se falhar, cai num `where=""` e no traceback
  via `formatException`.
- `FlareMiddleware`: captura opt-in dos headers da request e da response
  (`capture_request_headers` / `capture_response_headers`), enviados como os
  atributos `request_headers` / `response_headers` — cada atributo é um dict `{nome: valor}`
  (nomes em minúsculas, valores decodificados latin-1). Headers sensíveis
  (`authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key`,
  `x-auth-token`) têm o VALOR redigido para `"***"` — o nome permanece, o segredo
  não vaza para o dashboard. OFF por padrão.

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
