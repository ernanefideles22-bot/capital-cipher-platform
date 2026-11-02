# S3: revisão financeira e procedimento de ativação e recuperação

Estado desta entrega: **preparado para revisão; não aplicado**. Nenhum deploy,
flag, variável, serviço, volume, risco, OMS ou ordem é alterado por este PR.
O leitor financeiro é offline e não é importado pelo serviço/coletor.

## Ponto de partida e condição de liberação

| Item | Referência |
|---|---|
| Projeto Railway | `b21708b8-5703-49f0-9cbd-9ff74be94ce8` |
| Ambiente staging | `2f8f48c0-ed89-4ce4-bbb6-50df36a62680` |
| Serviço Gate | `c75db2ba-251b-4af9-97b5-a05b9b7da4ea` |
| Deployment S1+S2 sob observação | `ac46559c-5be2-4a8f-99a5-f7062f9c80f4` |
| Commit da integração | `8f62ed079b73c1d89e570a3ff7b9be45c1d6a58f` |
| Início / primeiro ciclo UTC | `2026-11-02T11:13:20Z` / `2026-11-02T11:13:37Z` |
| Volume a preservar | `7f0dfa7c-1db9-4cef-8f2e-19baf8688dc5`, `/gate-data`, 1024 MB |
| Runtime S3 preparado e testado | PR #82, commit `28fd9ce1f618cc271fd0bd1188ac7f4968057feb` |
| Ensaio de recuperação/escala, sem mudança no runtime | PR #83, commit `6b954e7c377485cba21d0868910ec055ba858277` |

Antes de aplicar qualquer mudança, exigir a homologação revisada das primeiras
24h **deste** deployment S1+S2, com logs completos e identidade da versão.
O primeiro ciclo completa 24h em **03/11/2026 07:13:37 America/Manaus**;
o horário sozinho não aprova nada. Reinício/troca reinicia a janela. Os 109
ciclos da versão anterior de S1 não contam. Este documento não declara a
homologação concluída nem autoriza execução de ordens.

## O que o coletor registra e o que falta

O código revisado está em `backend/app/demo/gate_s3_collector.py`,
`backend/app/strategy/gate_s3_durable_research.py` e
`backend/app/strategy/gate_s3_forward_scorecard.py`.

| Evidência | Comportamento do código |
|---|---|
| Fonte | Candle 15m fechado e hipóteses públicas do mesmo snapshot S1/S2; nenhum fetch adicional do coletor |
| Proveniência | Mercado, OHLCV, fechamento/recebimento, cutoff da análise opcional e disponibilidade real da decisão |
| Política | Identificador/commit, estratégia/versão, timeframe, ação proposta e filtrada, stop/alvo e motivos de rejeição |
| Entrada hipotética | Abertura posterior estritamente à observação/decisão disponível; nenhuma entrada retroativa |
| Saída hipotética | Stop, alvo e regra temporal do scorer congelado; preços, quantidade, custos e motivo persistidos |
| Contas | BTC/ETH/SOL × três estratégias clássicas × STANDALONE/S1_CONFLUENCE; 18 contas independentes, não uma carteira |
| Custos padrão | 6 bps por lado de comissão e 6 bps por lado de slippage; funding modelado 1 bp/8h, mínimo de um intervalo |
| Risco padrão | Capital hipotético 1000 por conta; risco 0,25% por trade; teto de notional 100%; perda diária 2%; drawdown máximo 10% |
| Integridade | Evento canônico, hash anterior/atual por mercado, outcome e cabeçalhos; replay determinístico verificado |
| Persistência | SQLite WAL separado em `/gate-data/capital-cipher-gate-s3/forward-v1.sqlite3`, escritor exclusivo |

O diário não preserva as 200 barras completas de 1h/4h que produziram a
leitura S1. O replay financeiro confirma as hipóteses registradas, não
reconstitui independentemente toda a análise técnica. Hashes e triggers
detectam inconsistência; não constituem atestação criptográfica externa contra
reescrita integral por administrador. Não há fills reais, preço de liquidação,
retorno de carteira, latência de ordem ou drawdown intrabar comprovados.

## Relatório offline: entrada e interpretação

Sobre uma exportação consistente do diário existente, executar a partir de
`backend`, usando este código de pesquisa:

```bash
python -m scripts.gate_s3_financial_review /analysis/source/forward-v1.sqlite3 \
  --output /analysis/reports/review-20261103 \
  --as-of 2026-11-03T12:00:00+00:00
```

Os caminhos e o horário acima são exemplos; substituir pelo snapshot e horário
reais. `financial-review.json` inclui custos, expectativa, fator de lucro,
drawdown e comparações fechadas por conta. `financial-review.md` apresenta o
quadro principal. A saída deve estar fora do diretório da fonte.

O leitor abre arquivo existente com `mode=ro` e `query_only`, verifica o
replay original e extrai trades em outra transação somente de leitura. Exige
contagem/hash final idênticos entre as duas leituras; se o escritor avançar,
rejeita e pede nova tentativa. Não modifica nem pausa o coletor. SQLite pode
usar os arquivos normais de coordenação WAL. Arquivo ausente, corrompido ou
prefixo divergente gera `EVIDENCE_UNAVAILABLE`, sem inventar PnL zero.

- Líquido encerrado = bruto **já após slippage** menos comissões e funding.
  Slippage não é descontado novamente. O relatório não calcula o contrafactual
  de uma estratégia sem slippage, nem muda as premissas congeladas.
- Expectativa = líquido realizado / operações encerradas; sem trades é `null`.
- Fator de lucro líquido = soma dos trades líquidos positivos / módulo da soma
  dos negativos, usando valores persistidos arredondados a oito casas.
  Sem perdas, não se imprime infinito nem se declara aprovação: valor `null`
  e estado `NO_CLOSED_LOSSES`; sem trades, `NO_CLOSED_TRADES`.
- `last_closed_trade` se repete nos snapshots: o leitor conta somente aumento
  de `completed_trades`, reconcilia os totais e não duplica encerramentos.
- Estimativas de saída de posições abertas permanecem separadas dos resultados
  encerrados. Drawdown entre fechamentos pode ignorar perdas intrabar.
- `--as-of` exige fuso explícito. Fonte acima de 30 minutos é `STALE`;
  sem horário explícito, freshness é `NOT_EVALUATED`. Isso não atualiza o preço.
- Compara líquido encerrado de original/confluência/caixa por estratégia e
  mercado, sem pooling. Reduzir prejuízo contra original não é superar caixa.
- Piso de 96 candles 15m pós-freeze e 30 encerramentos **em cada braço** é
  apenas descritivo. Não prova significância, regimes ou robustez sob estresse;
  todos os indicadores de autorização/lucro comprovado permanecem falsos.

O endpoint existente do futuro runtime S3 entrega seu relatório em cache;
estas métricas adicionais são do CLI offline, **não** foram ligadas ao servidor.
Não existe relatório financeiro prospectivo desta entrega antes da ativação e
da coleta. Testes usam fixtures sintéticas, sem representar resultado de mercado.

## Preparação da ativação — executar somente após revisão/liberação

1. Registrar homologação S1+S2, deployment/commit atual, flags não secretas,
   volume e inventário. Confirmar `/health` UP/READ_ONLY, S1/S2 AVAILABLE 3/3,
   freshness até 30 min, execução/canário/autorizações falsos, OMS sem liberação,
   storage AVAILABLE e recovery CLEAR. Qualquer alerta bloqueia a mudança.
2. Revisar o candidato exato PR #83 (inclui #82), sua CI e compatibilidade com
   o serviço. Esta entrega empilha somente leitor/testes/documentação sobre #83;
   não exige importar experimentos históricos dos PRs #84–86 no runtime.
3. Confirmar uma réplica, mesmo volume/mount/tamanho, espaço livre e cópia de
   segurança consistente com capacidade disponível. A reserva de 128 MiB é
   guarda do código, não espaço livre atualmente certificado por este documento.
   Não duplicar banco WAL ativo copiando só o arquivo `.sqlite3`: usar exportação
   SQLite coerente por procedimento revisado, ou parada limpa autorizada.
4. Preparar em uma única mudança revisável o código candidato e flags abaixo.
   Preservar a conexão OMS de leitura e as proteções existentes; não mudar
   chaves, risco, grants, modo LIVE/canário ou arquivos legados. Não existe
   variável `GATE_S3_ROOT`: o coletor utiliza a raiz persistente já verificada.

| Flag da futura mudança | Valor pretendido |
|---|---|
| `GATE_S1_SHADOW_ENABLED` | `true` |
| `GATE_S2E_SHADOW_ENABLED` | `true` |
| `GATE_S3_SHADOW_ENABLED` | `true` |
| `GATE_OMS_WRITES_ENABLED` | `false` |

5. Somente após liberar a mudança concreta, aplicar e registrar o novo ID/commit
   e início. A nova versão tem sua própria janela de estabilidade. Nenhuma parte
   desta etapa foi aplicada na preparação deste PR.
6. Conferir `/health`, `/api/gate/status` e `/api/gate/research/forward`;
   verificar S1/S2 3/3, S3 sem BLOCKED, worker/fila, observações **confirmadas**
   crescentes para três mercados, timestamps frescos e proteção de execução.
   `QUEUED_NOT_COMMITTED` não é persistência. `NO_ELIGIBLE`/amostra insuficiente
   iniciais não são rentabilidade zero nem defeito financeiro.
7. Confirmar inventários sem posições/ordens/condicionais. Isso não substitui
   auditoria exaustiva da corretora. Preservar os arquivos de pesquisa antigos
   `capital-cipher-gate-testnet/research.sqlite3` e `cycles.sqlite3`.

O opt-in S3 ancora a amostragem em fronteiras UTC de 15m + 10s, em vez de
sleep acumulado de 900s. Verificar a nova cadência após a mudança. Fila limitada
a 12 itens de até 32 KiB; fila cheia ou falha de escrita/relatório bloqueia S3,
independentemente de S1/S2. A capacidade é **10.000 observações somando mercados**:
a 288/dia, cerca de 34,7 dias. Planejar revisão em 8.000 (80%, cerca de 27,8 dias);
isso é limiar operacional proposto, não alerta já instalado. Não apagar, rotacionar
ou ampliar limite automaticamente. Estratégias raras podem não atingir o piso
de trades antes disso; uma janela maior exige revisão de capacidade e política.

## Falha, recuperação e reversão

| Situação | Procedimento preparado |
|---|---|
| S3 BLOCKED/fonte antiga | Manter ordens desligadas; inspecionar código fixo de erro e timestamps; não publicar financeiro obsoleto como atual |
| Reinício compatível | Reabrir com a mesma versão compatível: replay verifica prefixo confirmado e restaura posições hipotéticas. Eventos só enfileirados podem se perder |
| Gap de candle após indisponibilidade | Bloqueio é esperado: não inventar receipts/decisões históricos. Exigir avaliação de nova janela/migração auditada; o caminho atual é fixo e não tem reset automático |
| Corrupção/hash/head/schema divergente | Preservar banco, WAL/SHM e diretório/lock como evidência; não reparar, truncar, apagar ou reinicializar para obter verde |
| Limite/reserva de disco | Bloquear coleta e revisar capacidade/retention explicitamente; sem exclusão de evidências nem contratação automática |
| Reversão para `8f62ed0` | Desativar S3 antes ou na mesma mudança autorizada; manter S1/S2 e writes=false. Código anterior não deve abrir o novo diário; preservar todo o diretório S3 |

Desativação/reversão são procedimentos de uma mudança futura separadamente
autorizada, não ações deste PR. Reiniciar indefinidamente não substitui análise
de causa. Após recuperação exigir mesma evidência de integridade, freshness,
disponibilidade e proteção de ordens. Não liberar TESTNET/LIVE como resposta a
S3 bloqueado ou a resultado hipotético positivo.

Referências: PRs #80–83, `docs/gate-s3e-optin-collector-20261102.md` e
`docs/gate-s3f-process-crash-and-scale-20261102.md`. Nenhuma conclusão de
lucratividade ou homologação hospedada é produzida por testes de software.
