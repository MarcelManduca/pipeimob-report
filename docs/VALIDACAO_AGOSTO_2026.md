# Validação controlada — Pipeimob × CRM Vista

Período: 01/08/2026 a 20/08/2026.

Este relatório usa os CSVs fornecidos para validar a regra antes da conexão ao vivo.
Nenhum nome, e-mail, telefone ou identificador de cliente foi incluído.

## Resultado

| Indicador | Resultado |
|---|---:|
| Vendas oficiais no Pipeimob | 19 |
| VGV oficial | R$ 24.196.504,00 |
| Ganhos do Vista conciliados | 6 |
| Pipeimob sem Ganho correspondente no Vista | 13 |
| Vista sem contrato no Pipeimob | 0 |
| Divergências de valor | 0 |
| Divergências de data acima de 7 dias | 0 |
| Vínculos ambíguos | 0 |
| Dados essenciais incompletos | 0 |

## Evidências técnicas

- O arquivo Pipeimob contém 19 transações no período.
- O arquivo Vista contém 6 registros com `Status = Ganho` e `Etapa = Fechamento`.
- Os 6 Ganhos foram vinculados a transações Pipeimob pelo código do imóvel.
- Valores dos 6 pares são iguais.
- O maior atraso observado entre a Data CCV e o encerramento/Ganho do Vista foi de 3 dias.
- O campo `Encerramento do negócio` é a data utilizável do Ganho nesse CSV.
- O campo `Última atualização (em dias)` contém idade em dias, não uma data, e não deve ser usado como data da venda.

## Regra confirmada

1. Uma venda existe quando há contrato/transação assinada no Pipeimob.
2. O Vista confirma o registro comercial somente quando `Status = Ganho`.
3. `Etapa = Fechamento` sem `Status = Ganho` não é venda.
4. A data oficial e o VGV vêm do Pipeimob.
5. O corretor comercial vem do Vista; o corretor fiscal do Pipeimob permanece em campo separado.

## Limitação desta rodada

Os 13 contratos sem Ganho no Vista podem representar atraso operacional, ausência de
marcação ou negócio não localizado no recorte exportado. A classificação definitiva
depende da consulta ao vivo ao Vista com cobertura do intervalo e tolerância de datas.
