# S3F — ensaio isolado de interrupção de processo e escala

**Preparação de pesquisa, sem merge/deploy e sem mudanças no Railway.**

Runtime avaliado: commit `28fd9ce1f618cc271fd0bd1188ac7f4968057feb` do PR #82. Este trabalho acrescenta somente o ensaio, testes, CI e evidências; não modifica o coletor, estratégia, risco, custos, banco existente ou permissões de execução. Os fingerprints do runtime e do harness estão em [baseline.json](research/gate-s3f-20261102/baseline.json).

## Interrupções reais

O harness cria processos filhos e bancos temporários próprios. SIGKILL é enviado somente aos filhos que ele iniciou. Um processo novo verifica o diário, restaura posições hipotéticas, recebe um reenvio duplicado e continua a sequência. A referência sem interrupção e um cálculo financeiro separado precisam coincidir.

| Ponto de interrupção | Eventos confirmados visíveis | Eventos publicados no cache | Recuperação em processo novo (ms) | Eventos finais |
|---|---:|---:|---:|---:|
| Posição hipotética aberta | 9 | 9 | 19.679 | 12 |
| INSERT feito, transação não confirmada | 9 | 9 | 16.694 | 12 |
| COMMIT concluído, antes de atualizar memória | 10 | 9 | 20.678 | 12 |

**Resultado: 3/3 cenários aprovados.** O teste anterior ao commit tinha 4.152 bytes de WAL, incluindo frames de uma transação não confirmada. O caso pós-commit preservou o evento 10 mesmo com cache ainda no evento 9. Todos terminaram com 12 observações e exatamente um reenvio duplicado descartado, com relatório idêntico à referência e diferença financeira máxima de 0,00000000 na precisão de 8 casas.

Os testes também conferem bruto após slippage, comissão, funding e líquido contra respostas numéricas conhecidas do fixture. O cenário contém LONG, SHORT e um braço filtrado que fica em caixa; não é uma amostra de mercado ou prova de lucro.

Pacotes sintéticos conhecidos são reenviados pelo emissor do ensaio. Isso não significa que dados só enfileirados sobrevivam, nem autoriza recuperar candles reais perdidos inventando receipts; lacunas reais continuam sujeitas ao bloqueio do S3.

## Escala e memória

Ambiente medido: Linux x86_64, Python 3.12.14, SQLite 3.53.1. Um processo novo por tamanho. Cada histórico inicial recebeu mais 20 observações pela API real do coletor, com consulta à rota ASGI real em memória. Apenas S1/S2/fetch e OMS são substituídos por emissores locais, sem rede ou contas.

| Histórico inicial | Eventos finais | Reinício até API S3 pronta (s) | RSS máximo do processo (MiB) | Diário ao fechar (MiB) |
|---:|---:|---:|---:|---:|
| 100 | 120 | 0.263 | 78.4 | 0.66 |
| 1000 | 1020 | 1.102 | 87.3 | 5.38 |
| 5000 | 5020 | 3.467 | 121.3 | 26.41 |
| 9900 | 9920 | 6.728 | 162.8 | 52.17 |

| Histórico inicial | Enfileirar P50/P95/P99 (ms) | Gravar e publicar relatório verificado P50/P95/P99 (ms) | GET do cache P50/P95/P99 (ms) | GET durante reconstrução P99 (ms; n) |
|---:|---:|---:|---:|---:|
| 100 | 0.182 / 0.540 / 0.604 | 119.654 / 135.952 / 138.417 | 6.912 / 13.318 / 14.116 | 43.272; n=5 |
| 1000 | 0.165 / 0.331 / 0.569 | 419.070 / 783.127 / 791.992 | 6.550 / 11.807 / 12.416 | 174.027; n=19 |
| 5000 | 0.176 / 0.496 / 0.534 | 1680.485 / 2419.506 / 2556.508 | 7.208 / 15.680 / 40.519 | 490.538; n=73 |
| 9900 | 0.170 / 0.574 / 0.603 | 3206.734 / 3863.811 / 3902.396 | 6.569 / 10.082 / 10.868 | 504.094; n=131 |

Na maior carga, a recuperação levou 6,728 s, o RSS atingiu 162,8 MiB e publicar uma observação com relatório verificado chegou a P99 de 3,902 s. Após reconstrução, GET do cache teve P99 de 10,868 ms. Durante a reconstrução, o P99 observado de GET foi 504,094 ms; a lentidão do boot não deve ser confundida com a latência do cache pronto.

O custo de atualização cresce com o histórico: a implementação atual refaz o replay/verificação para gerar o cache financeiro. O limite de 10.000 observações continua importante. Ampliar esse limite exige outra avaliação e possivelmente agregação incremental; este ensaio não autoriza removê-lo.

## Método e limites da evidência

- Dados determinísticos sintéticos, três símbolos, um padrão de entrada por símbolo, três estratégias/braços independentes. Não mede os 300 agentes, o broker, a internet, slippage real, lucro ou fills.
- Fixtures maiores são carregados em lote apenas para preparar o histórico. Todas as conexões de preparação são fechadas; o coletor real depois verifica cada hash, resultado e cabeçalho. O tempo dessa carga em lote não é throughput do serviço.
- A recuperação medida exclui inicialização do interpretador/dependências. A memória é RSS máximo do processo novo depois das importações; inclui a aplicação ASGI e o worker, mas não o processo que montou o fixture.
- O sistema operacional pode manter páginas em cache. Não é ensaio de disco físico frio, falta de energia, kernel panic, falha do dispositivo ou filesystem do Railway.
- Percentis por nearest-rank. Com 20 amostras, o P99 das gravações/consultas é o maior valor observado, não uma estimativa robusta da cauda de produção. A quantidade de requests durante o boot varia com sua duração e consta na tabela.
- SQLite, filesystem e agendamento de CPU do ambiente local diferem do Railway. Antes de ativar S3, ainda é necessária validação no runtime autorizado de disco, recuperação, lacunas e latência.
- Orçamentos locais provisórios, declarados antes da medição: fila P99 <=50 ms, cache HTTP P99 <=200 ms, recuperação <=60 s, publicação P99 <=30 s e RSS <=256 MiB. Todos foram atendidos nesta execução. Eles não são SLA hospedado, critério de lucro nem autorização para operar.

## Reproduzir

Em Linux/POSIX, com as dependências do backend instaladas:

```bash
cd backend
python scripts/gate_s3_isolated_drill.py --sizes 100,1000,5000,9900 --samples 20 --output /tmp/s3-drill.json
python -m pytest -q app/tests/test_gate_s3_process_drill.py
```

O comando principal cria e remove seus próprios TemporaryDirectory. Variáveis/chaves da corretora do processo pai não são encaminhadas aos filhos. Somente flags PAPER/OMS desativado e o ambiente mínimo do Python são passados. As rotas de rede e o OMS são substituídos no ensaio; nenhuma automação, publicação, variável ou volume do Railway é alterado.

Regressão local: **223 testes aprovados e 1 ignorado**, incluindo sete casos novos do ensaio de processo/escala/oráculo.

O workflow dedicado repete as interrupções/reconciliação e regressões S1/S2/S3/OMS, além de uma caracterização menor no runner. A caracterização do runner é outra medição, não substitui os valores locais deste relatório. CI geral precisa passar no HEAD publicado. O resultado continua sendo pesquisa; liberação operacional e permissões de ordens são decisões separadas.
