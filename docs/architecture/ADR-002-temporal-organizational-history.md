# ADR-002 — histórico organizacional temporal e soft delete

- **Estado:** Aceita como desenho; persistência pendente
- **Data:** 2026-09-06

## Contexto

Vista e Pipeimob podem deixar de retornar pessoas e equipes inativadas. Pessoas
também mudam de equipe, equipes mudam de gerente ou loja, e vendas podem sofrer
correções. Recalcular o passado pela estrutura atual destrói o legado individual
e altera resultados já consolidados.

## Decisão

- Entidades possuem identidade estável separada de nome e estado atual.
- Relações pessoa→equipe e equipe→loja usam vigência `[valid_from, valid_to)`.
- Vendas guardam uma fotografia organizacional e versões imutáveis do fato.
- Inativação ou ausência nunca apaga entidade, vínculo encerrado ou resultado.
- Ausência somente produz `source_missing` após snapshot completo.
- Correção da fonte encerra a versão vigente e cria uma nova.
- Conflitos simultâneos permanecem explícitos; mudanças em datas distintas são
  evidência histórica.

## Consequências

- Consultas históricas exigem data efetiva.
- Sincronizações precisam declarar completude por fonte.
- O esquema temporal aumenta o volume, mas torna o resultado explicável.
- A migração candidata deve ser validada em Supabase isolado antes de produção.

## Critério de revisão

Revisar somente se as fontes fornecerem histórico oficial completo ou se uma
regra contábil/comercial exigir outro tratamento de correção retroativa.
