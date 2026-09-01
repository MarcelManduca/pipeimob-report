# Gralha Indicadores — implantação

Esta pasta contém as migrações e as Edge Functions do Gralha Indicadores.
Nada nesta pasta é uma fonte alternativa para fatos de venda. A arquitetura,
o modelo de acesso e a publicação estão documentados em
[`docs/GRALHA_INDICADORES_ARCHITECTURE.md`](../docs/GRALHA_INDICADORES_ARCHITECTURE.md).

## Hierarquia de dados

1. Pipeimob ao vivo: quantidade, data oficial e VGV.
2. Vista ao vivo: corretor comercial e, quando o tenant confirmar o campo,
   equipe específica do negócio.
3. Grupos oficiais do responsável no Pipeimob: equipe nativa da API.
4. `manager_team_reference`: fallback de responsável/gerente para equipe,
   com vigência derivada da interpretação gerencial da planilha.

A antiga associação por corretor + data + imóvel não é usada. Resultados de
validação, nomes, contagens e históricos internos devem permanecer fora do
controle de versão público.

## Publicação e validação

Seguir a ordem de publicação da documentação de arquitetura. Validar o projeto
de destino antes de aplicar migrações ou publicar funções. A ampliação da
identificação corretor→equipe é uma fase separada, pausada até o comando
`INICIAR-VALIDACAO-EQUIPES-GRALHA`.

## Respostas e autorização

A Edge Function orienta o consumidor a responder primeiro com o número,
ranking ou conclusão solicitada. Não existe prefixo obrigatório. Perguntas
objetivas devem ser curtas; contexto gerencial, cobertura e fonte entram apenas
quando ajudam a interpretar o resultado ou quando são solicitados.

Erros do backend são devolvidos de forma estruturada para que o Worker apresente
uma falha curta, sem montar análises com valores ausentes. A função limita a
espera pelo backend a 35 segundos.

`verify_jwt=false` desativa apenas a validação automática do gateway. A função
mantém a autenticação interna: valida o bearer token com `auth.getUser`, exige
perfil ativo e consulta `profiles.access_role` e `user_team_access`. CEO, CSO e
CMO têm acesso geral; diretores acessam as equipes selecionadas e gerentes
acessam exatamente uma equipe. O resumo do funil e seus gráficos são
recalculados no MCP a partir das equipes autorizadas. A função
`gralha-portal-admin` aplica a autorização executiva à gestão de usuários.
