# ADR-001 — autoridade dos dados e limites dos componentes

- **Estado:** Aceita
- **Data:** 2026-09-06

## Contexto

O portal combina autenticação, conversação, indicadores e duas fontes
operacionais. Sem uma autoridade explícita, divergências podem produzir dupla
contagem, reatribuição indevida ou regras diferentes entre serviços.

## Decisão

- Pipeimob é autoridade de quantidade, data de CCV e VGV das vendas.
- Vista é autoridade do diretório organizacional vigente e do funil comercial.
- Supabase é autoridade de identidade do portal, RBAC persistido e histórico.
- O MCP aplica o escopo autorizado e retorna agregados.
- O Render normaliza e concilia fontes; não decide acesso do usuário final.
- A OpenAI interpreta e redige; não é fonte oficial de dados.
- Nomes nunca substituem IDs estáveis.

## Consequências

- Divergências devem ser exibidas e auditadas.
- Nenhum componente pode assumir responsabilidade de outro por conveniência.
- Contratos entre serviços precisam declarar fonte, período, completude e versão.
- A substituição integral do Power BI exige validação de cobertura, não apenas
  semelhança visual dos resultados.

## Critério de revisão

Revisar se uma fonte deixar de fornecer o dado com estabilidade, se outra fonte
passar a ter maior autoridade comprovada ou se houver alteração contratual da
integração.
