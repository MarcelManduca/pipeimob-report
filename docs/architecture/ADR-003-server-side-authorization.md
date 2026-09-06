# ADR-003 — autorização em camadas no servidor

- **Estado:** Aceita
- **Data:** 2026-09-06

## Contexto

Executivos possuem visão geral, enquanto diretores e gerentes veem somente seus
escopos. Restringir botões no navegador não impede chamadas diretas às APIs.

## Decisão

- Supabase Auth valida identidade e sessão.
- Postgres RLS isola perfis, conversas e mensagens.
- O MCP aplica RBAC por cargo e equipes antes de consultar indicadores.
- A função administrativa valida token e cargo executivo antes de usar
  `service_role`.
- O backend Render aceita somente credenciais de serviço autorizadas e devolve
  dados agregados e sanitizados.
- A interface reflete permissões, mas não é a autoridade de segurança.

## Consequências

- Toda nova rota precisa de classificação de autenticação e autorização.
- Testes devem cobrir acesso permitido, negado e tentativa fora do escopo.
- Falhas de autorização não podem ser compensadas pela OpenAI ou pelo frontend.

## Critério de revisão

Revisar ao criar novo cargo, novo consumidor de API, nova tabela ou nova forma de
compartilhamento entre usuários.
