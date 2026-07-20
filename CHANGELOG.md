# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/);
o projeto segue [SemVer](https://semver.org/lang/pt-BR/).

## [0.1.0] — não lançado

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
