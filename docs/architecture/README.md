# Arquitetura — índice de decisões

Este diretório registra decisões estruturais do Gralha Indicadores. O documento
geral está em [`../GRALHA_INDICADORES_ARCHITECTURE.md`](../GRALHA_INDICADORES_ARCHITECTURE.md)
e as regras temporais detalhadas estão em
[`../GRALHA_LIVE_TEAM_HISTORY.md`](../GRALHA_LIVE_TEAM_HISTORY.md).

## ADRs vigentes

| ADR | Decisão | Estado |
|---|---|---|
| [ADR-001](./ADR-001-data-authority-and-boundaries.md) | Autoridade das fontes e limites dos componentes | Aceita |
| [ADR-002](./ADR-002-temporal-organizational-history.md) | Histórico organizacional temporal e soft delete | Aceita como desenho; persistência pendente |
| [ADR-003](./ADR-003-server-side-authorization.md) | Autorização em camadas no servidor | Aceita |

## Regra de manutenção

Um ADR não é apagado quando a decisão muda. Ele recebe estado `Substituída` e
aponta para o ADR sucessor. Mudanças de autoridade, segurança, persistência,
semântica temporal ou fronteira de serviço exigem novo ADR ou atualização
explícita de estado.
