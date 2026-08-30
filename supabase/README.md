# Gralha Indicadores — implantação

Esta pasta contém a migração e a Edge Function preparadas para a próxima
publicação. Nada nesta pasta é uma fonte alternativa para fatos de venda.

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

## Ordem de publicação

1. Revisar registros marcados com `review_required=true` em ambiente privado.
2. Aplicar a migração de `manager_team_reference`.
3. Publicar o backend com o contrato de conciliação correspondente.
4. Validar a resposta do backend em um período conhecido.
5. Publicar `gralha-indicadores-mcp` com a configuração de autenticação.
6. Executar testes autenticados de ranking por corretor, equipe e bairro.

`verify_jwt=false` desativa apenas a validação automática do gateway. A função
mantém a autenticação interna: valida o bearer token com `auth.getUser`, exige
perfil ativo e restringe o acesso aos papéis `super_admin` e `viewer`.
