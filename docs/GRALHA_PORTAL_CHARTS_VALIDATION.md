# Correção do portal: gráficos contextuais e carregamento do perfil

## Escopo

Base: `main`, commit `e53627b`. Correção independente do PR #33 (isolamento de agregados do funil). Somente Worker, testes e documentação; sem migrações, alterações de permissões, Edge Functions ou ampliação corretor→equipe.

## Comportamento

- “Crie um gráfico”, seguido de “Sim” ou “ambos”, conserva a pergunta de referência dentro da janela de contexto. Uma nova pergunta substantiva encerra essa intenção.
- Até 40 mensagens recentes são consideradas, em vez de 10 no servidor e 12 no cliente. O cliente limita esse histórico a 45 KB para respeitar o limite da API. Não há recuperação ilimitada de conversas nem reutilização dos valores escritos pela IA como dados de gráfico.
- Rankings mensais genéricos de bairros, corretores ou equipes podem consultar diretamente o MCP com o token do usuário. Nomes, filtros, períodos arbitrários e comparações continuam no caminho MCP/OpenAI existente; não são descartados para formar uma consulta geral.
- O circuito de disponibilidade também considera o assunto da pergunta original de um gráfico.
- Gráficos usam apenas o campo estruturado `visualization` retornado pelo MCP. Texto livre, JSON inventado e especificações `grouped_bar` da resposta não são executados nem convertidos em dados.
- Quantidade é o padrão. “Ambos” usa ranking por quantidade e dois painéis SVG com as mesmas categorias, escalas independentes e rótulos com unidades. Continua valendo o limite visual de 10 itens. Trocar para VGV faz uma nova consulta pelo critério correto, sem reordenar um Top 10 parcial anterior.
- O contrato interno `sales_comparison` aceita somente categorias e métricas finitas, não negativas e presentes. Ausências não viram zero. Formatos incompatíveis geram uma mensagem clara, sem repetir a escolha de formato.
- Os novos gráficos são persistidos junto das novas respostas e reabertos pelo mesmo renderizador. Conversas antigas não são apagadas nem reescritas; uma resposta antiga que contém JSON precisa de um novo pedido de gráfico.
- O perfil sai do estado de carregamento em caso de erro HTTP, falha de rede, JSON inválido ou timeout de 15 segundos. A interface oferece uma tentativa manual e mantém a gestão de usuários oculta até confirmação explícita do perfil.
- Respostas atrasadas do perfil não restauram controles de uma sessão encerrada. O histórico pode carregar mesmo quando o perfil falha.
- “Acesso verificado” indica apenas que o perfil foi confirmado; não representa um diagnóstico de saúde de todas as integrações.

## Apresentação mobile

- Até 600 px de viewport, rankings de barras usam uma lista HTML responsiva com nomes completos e valores em 15 px, barra de 22 px abaixo de cada cabeçalho e quebra de linha para nomes/valores longos. O SVG de desktop fica oculto nesse breakpoint; a lista mobile fica oculta em telas maiores. Quantidade e VGV mantêm dados, ordem e escalas independentes.
- Títulos usam 18 px e notas/legendas 13 px no celular. O tamanho do texto não depende da redução do `viewBox` de desktop. Funis continuam com o renderizador existente.
- Até 900 px, o botão de histórico fica dentro do cabeçalho sticky, com espaço reservado no fluxo da página. A gaveta tem fechamento próprio, fundo clicável e Escape; quando fechada fica `inert`, e a navegação volta ao estado normal no desktop.
- O campo de pergunta usa placeholder curto e fonte de 16 px no mobile, com botões de 44 px.
- A lista mobile é montada pelo mesmo renderizador ao abrir gráficos salvos. Não é necessário reescrever o histórico para aplicar esse ajuste de apresentação.
- As capturas recebidas confirmam os gráficos de quantidade e VGV na versão anterior da prévia. A legibilidade desta revisão mobile ainda precisa de conferência visual no dispositivo. Não desconsiderar aviso de certificado TLS ao autenticar; a causa do aviso observado não foi verificada por esta alteração.

## Testes locais sem serviços externos

```sh
node --check cloudflare/gralha-indicadores-chat-worker-v11.js
node --test tests/worker_circuit_breaker.test.mjs tests/worker_portal_charts.test.mjs
git diff --check
```

Os testes usam exclusivamente dados sintéticos, respostas HTTP simuladas e um DOM mínimo. Executam o handler real do Worker, a persistência simulada, o JavaScript efetivamente servido e as funções de renderização/perfil. Não certificam o layout final em um navegador autenticado nem a correção dos números reais.

## Validação antes de produção

1. Na prévia do Worker, consultar bairros de um mês e pedir “Crie um gráfico”; confirmar renderização sem perguntas de tamanho/formato.
2. Pedir “ambos”; conferir os dois painéis, as unidades, a mesma ordenação e a nota de período/escopo.
3. Pedir “Top 5 por VGV”; conferir nova ordenação contra a fonte autorizada.
4. Reabrir a conversa pelo histórico e verificar os gráficos. Conferir também Home e Nova conversa.
5. Conferir filtros por nomes e troca de assunto, sem reutilização do gráfico anterior.
6. Verificar visualmente desktop/mobile, categorias longas e valores monetários grandes.
7. Com conta autorizada, testar erro e recuperação do perfil sem convidar usuários nem alterar cargos.
8. Validar os escopos de gerente, diretor e executivo. Este PR não substitui a correção de RBAC do PR #33 nem certifica sua publicação.

O PR deve permanecer sem merge e sem publicação em produção até autorização. A integração GitHub/Cloudflare pode criar automaticamente uma prévia de branch; essa prévia não muda a `main` e utiliza os serviços existentes. Não aplicar migrações nem publicar Edge Functions para esta correção.
