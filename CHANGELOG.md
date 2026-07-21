# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
o projeto segue [SemVer](https://semver.org/lang/pt-BR/).

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
