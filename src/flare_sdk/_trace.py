"""
O ``trace_id`` da transação em curso — o elo entre o log e a request.
=====================================================================

O Flare guarda ``trace_id`` como COLUNA nas duas tabelas (``flare_logs`` e
``flare_requests``). É o que permite, no dashboard, sair de um erro e abrir a
request que o causou — e a volta. Sem ele os dois sinais existem lado a lado e
não se tocam: a tela mostra o erro, mostra as requests, e não há como afirmar
qual request produziu qual erro.

Por que um ``ContextVar`` e não um argumento
--------------------------------------------
Quem sabe o id é o MIDDLEWARE (ele nasce junto com a request); quem precisa dele
é cada ``logger.error(...)`` espalhado pelo código. Passar o id por parâmetro
significaria enfiá-lo em toda assinatura entre os dois — e bastaria uma função
esquecer para o log daquele ponto sair órfão.

Um ``ContextVar`` é o canal que o ``asyncio`` já tem para isso: cada task (e cada
thread do pool que o FastAPI usa para endpoint síncrono) enxerga o valor de quem
a criou, e duas requests concorrentes não se misturam — que é exatamente o erro
que uma variável global teria.

A direção importa: escreva de FORA para dentro
-----------------------------------------------
O middleware ESCREVE e o resto LÊ. O contrário não é confiável: um endpoint
declarado ``def`` (não ``async def``) roda num threadpool com uma CÓPIA do
contexto, então um ``set_trace_id`` lá dentro não volta para o middleware — a
request seria gravada sem o id que os logs usaram, e o elo quebraria em silêncio
justo nos endpoints síncronos.

Por isso a app deve LER (``get_trace_id()``) para reaproveitar o id nas próprias
mensagens, em vez de gerar o seu e tentar empurrá-lo para fora.
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from typing import Optional

#: O id da transação em curso. ``None`` fora de uma request (um script, um worker,
#: o import do módulo) — e ``None`` é o valor honesto ali: não há transação para
#: correlacionar, e inventar um id criaria um elo que não existe.
_TRACE_ID: ContextVar[Optional[str]] = ContextVar("flare_trace_id", default=None)


def new_trace_id() -> str:
    """Um id novo: 32 hex de um UUID4, sem os hifens.

    Sem hifens porque ele vai para uma query string (o link do dashboard) e para
    um campo de busca — quanto menos caractere que precise de escape, melhor. O
    tamanho é o do UUID4 de propósito: id curto colide, e uma colisão aqui não dá
    erro, dá dois eventos de transações diferentes amarrados como se fossem um.
    """
    return uuid.uuid4().hex


def get_trace_id() -> Optional[str]:
    """O id da transação em curso, ou ``None`` fora de uma.

    É isto que a app chama para reaproveitar o id do middleware nas próprias
    mensagens — assim o que aparece no log é o MESMO valor que foi para a coluna.
    """
    return _TRACE_ID.get()


def set_trace_id(value: Optional[str]) -> Token:
    """Fixa o id da transação. Devolve o token para o ``reset_trace_id``.

    Existe para quem já tem um id vindo de fora — o ``X-Request-Id`` de um gateway,
    o id de uma mensagem de fila — e quer que o Flare use o mesmo, em vez de gerar
    um segundo e desamarrar a transação do resto do rastro da empresa.

    **Chame de fora para dentro** (num middleware, no início do consumo da
    mensagem), nunca de dentro de um endpoint síncrono: ver o cabeçalho deste
    módulo.
    """
    return _TRACE_ID.set(value)


def reset_trace_id(token: Token) -> None:
    """Devolve o contexto ao estado anterior ao ``set_trace_id``.

    Restaurar pelo TOKEN, e não com ``set(None)``: em contexto aninhado (um
    middleware dentro de outro) o ``None`` apagaria o id do nível de cima em vez
    de devolver o que havia. O token é o que sabe qual era o valor anterior.

    Never-raise, e são DOIS erros distintos: ``ValueError`` quando o token veio de
    outro contexto, ``RuntimeError`` quando ele já foi usado (o ``ContextVar``
    aceita cada token uma vez só). Os dois significam a mesma coisa aqui — não há o
    que restaurar —, e nenhum pode derrubar uma request que já terminou de
    responder. Capturar só um dos dois deixava o outro escapar justo no caminho de
    limpeza, que é onde ninguém está olhando.
    """
    try:
        _TRACE_ID.reset(token)
    except (ValueError, RuntimeError):  # noqa: BLE001 — nada a restaurar.
        pass
