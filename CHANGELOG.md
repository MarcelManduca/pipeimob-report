# Changelog

## 2026-08-31 — Período conversacional e diagnóstico do Vista

- Preserva o mês mais recente citado na conversa e herda apenas o ano quando o
  usuário informa um novo mês sem repeti-lo.
- Separa a fotografia de negócios atualmente em Proposta da métrica histórica
  de propostas geradas.
- Responde diretamente às fotografias de Proposta e aos seus seguimentos por
  status, sem consumir tokens da OpenAI.
- Registra o status HTTP exato e sanitizado devolvido pelo Vista para permitir
  correções direcionadas da integração.
- Prepara o Worker Cloudflare v8 para publicação manual.

## 2026-08-30 — Resiliência da consulta de propostas

- Mantém uma resposta objetiva sobre a indisponibilidade do histórico de entrada
  em Proposta mesmo quando a fotografia ao vivo do Vista falhar.
- Recupera sequencialmente apenas as páginas recusadas durante a paginação
  concorrente do Vista.
- Trata desconexões remotas, resets de conexão e falhas SSL transitórias como
  erros recuperáveis antes de abandonar uma página do Vista.
- Reutiliza por até uma hora o último agregado recente quando a atualização do
  funil falhar, identificando a resposta como cache de contingência.
- Adiciona telemetria sanitizada para falhas do backend do funil.
- Atualiza a Edge Function `gralha-indicadores-mcp` para o contrato `1.12.0`.

## 2026-08-30 — Respostas gerenciais e latência

- Remove o prefixo obrigatório das respostas e prioriza conclusões diretas.
- Impede que erros técnicos sejam transformados em análises com valores ausentes.
- Adiciona caminho rápido para totais mensais de vendas e fotografias de propostas.
- Busca páginas conhecidas do funil Vista com concorrência limitada e retentativa curta.
- Reutiliza agregados do funil por três minutos para acelerar perguntas consecutivas.
- Reduz o esforço e o limite de saída do modelo em perguntas objetivas.
- Atualiza a Edge Function `gralha-indicadores-mcp` para o contrato `1.11.0`.
