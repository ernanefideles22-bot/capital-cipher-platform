# Capital Cipher — novas entradas/saídas e conferência da auditoria

## Resultado principal

**S1 homologada na auditoria de estabilidade; lucratividade ainda não validada.** A auditoria terminou em 02/11/2026 às 04h25 (MT), com 97 ciclos consecutivos disponíveis, BTC/ETH/SOL prontos e zero ordens/posições observadas. A conferência posterior encontrou ciclos 98–107 com sequência contínua e mais 28 inventários vazios.

A aplicação respondeu `UP`, `CONNECTED_READ_ONLY`, `execution_enabled=false`, `canary_execution_enabled=false`, `oms_release_authorized=false` e `oms_risk_clear=false`. O deployment continua `bd4b1ffb`, commit `2eb009b`, e o volume `/gate-data` de 1.024 MB está preservado. As três alterações S1+S2E continuam apenas preparadas. Nenhum deploy foi feito.

**Parecer: GO condicional para S1+S2 em SHADOW.** O conector oculta os valores das variáveis. O estado efetivo atual foi verificado pela API, mas o valor preparado para `GATE_S2E_SHADOW_ENABLED` ainda não foi confirmado. Isso impede afirmar que a nova integração está pronta para publicação sem essa conferência. Não se autoriza execução de ordens ou implantação neste relatório.

A auditoria original também informou dois serviços auxiliares EU2 em CRASHED, fora do escopo do serviço Gate. Zero ordens nos inventários examinados não substitui uma auditoria de todo o histórico da corretora. Metadados Railway com severidade `error` em mensagens `INFO` foram tratados pelo conteúdo funcional, conforme o parecer original.

Fontes e observações sanitizadas: `research/gate-s3i-20261102/audit-check.json`. A tarefa agendada foi concluída; não foi reiniciada nem reagendada.

## Protocolo das novas hipóteses

Protocolo `GATE_ENTRY_EXIT_RESEARCH_V1`, declarado às 06h43 MT (10:43 UTC), antes da execução destas hipóteses. Não alteramos as regras após observar o resultado:

1. `CONFIRM_CLOSE`: usar o sinal da barra anterior somente quando a próxima barra fechada avançar a favor da direção e não tiver tocado a distância de stop original medida a partir do fechamento do sinal. O sinal vence após essa única barra; a entrada hipotética acontece depois da confirmação. Stop e alvo originais são preservados.
2. `NET_RR2`: preservar a entrada e o stop; usar alvo `max(alvo original, 2 × stop + 3 × custo estimado)`. A equação busca recompensa líquida estimada de 2 para risco líquido estimado de 1: `(T-C)/(S+C) >= 2`. É uma estimativa por unidade baseada no fechamento conhecido, taxas/slippage e funding até o prazo máximo. Gaps, execução e duração real podem invalidá-la.
3. `CONFIRM_CLOSE_NET_RR2`: combinar as duas mudanças. As ablações separam o efeito da entrada do efeito da saída.

Stops não foram ampliados, risco e exposição não foram aumentados e o prazo máximo de permanência continua o original. Uma confirmação usa a distância de stop calculada no sinal anterior, não um stop recalculado para acomodar a oscilação posterior. Alvos maiores podem reduzir a taxa de acerto e aumentar saídas por tempo; isso foi medido pelo replay completo.

Custos base: taxa 6 bps/lado, slippage 6 bps/lado, funding 1 bp/8h. Estresse: taxa igual, slippage 12 bps/lado e funding 2 bps/8h. A estimativa usada pela regra não é recalibrada no estresse. Cada braço inicia conta hipotética independente de US$ 1.000, risco 0,25% e exposição máxima 1x, com os mesmos limites diários/drawdown. Resultados não representam uma carteira.

## Diagnóstico histórico concluído

São BTC, ETH e SOL × três estratégias × 15m/1h/4h: 27 combinações. O snapshot Testnet de 800 candles por série já foi observado nos PRs anteriores; portanto, esta etapa é exploratória. Diferentes timeframes cobrem períodos de calendário diferentes. Não se escolheu um novo limiar para melhorar esse histórico.

Controles: estratégia original, caixa e S1 nas entradas 15m. Escolha exclusivamente no trecho anterior de cada janela: mínimo de cinco operações encerradas, líquido positivo, fator de lucro >1 ou todas positivas, e desempenho superior a caixa e controles disponíveis. Empate pelo identificador da variante. Marcação estimada no fim da janela não conta como operação encerrada nem como lucro na escolha.

| Indicador | Resultado |
| --- | ---: |
| Combinações | 27 |
| Janelas cronológicas | 81 |
| Janelas que escolheram variante no treino | 15 |
| Janelas seguintes positivas / inativas / negativas | 2 / 3 / 10 |
| Janelas com evidência mínima suficiente | 0 |
| Variantes aprovadas para operar | 0 |

Os casos escolhidos no treino tiveram os seguintes resultados nas 120 barras seguintes. Valores são de operações encerradas, líquidos dos custos modelados; cada janela é uma conta separada:

| Mercado / estratégia | TF | Fim do treino | Variante | Trades | Líquido USD | Estresse USD |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| BTCUSDT / Donchian | 1h | 400 | CONFIRM_CLOSE | 1 | -1.281264 | -1.550407 |
| BTCUSDT / Donchian | 4h | 400 | NET_RR2 | 2 | -5.371639 | -5.533504 |
| ETHUSDT / Donchian | 1h | 400 | CONFIRM_CLOSE_NET_RR2 | 0 | +0.000000 | +0.000000 |
| ETHUSDT / Donchian | 1h | 520 | CONFIRM_CLOSE_NET_RR2 | 2 | -5.827301 | -6.134521 |
| ETHUSDT / Donchian | 1h | 640 | CONFIRM_CLOSE_NET_RR2 | 2 | -5.628885 | -5.851386 |
| ETHUSDT / Donchian | 4h | 640 | NET_RR2 | 5 | -4.835768 | -5.172274 |
| ETHUSDT / Bollinger | 4h | 520 | CONFIRM_CLOSE_NET_RR2 | 1 | -2.744542 | -2.866614 |
| ETHUSDT / Bollinger | 4h | 640 | CONFIRM_CLOSE_NET_RR2 | 2 | +3.053502 | +2.809321 |
| SOLUSDT / Donchian | 1h | 400 | CONFIRM_CLOSE | 0 | +0.000000 | +0.000000 |
| SOLUSDT / Donchian | 1h | 520 | CONFIRM_CLOSE | 1 | -2.905674 | -3.055245 |
| SOLUSDT / Donchian | 1h | 640 | CONFIRM_CLOSE | 2 | -5.575435 | -5.789924 |
| SOLUSDT / Donchian | 4h | 520 | CONFIRM_CLOSE_NET_RR2 | 2 | -5.312392 | -5.451729 |
| SOLUSDT / Bollinger | 4h | 400 | CONFIRM_CLOSE_NET_RR2 | 0 | +0.000000 | +0.000000 |
| SOLUSDT / Bollinger | 4h | 520 | CONFIRM_CLOSE_NET_RR2 | 1 | -2.697219 | -2.770138 |
| SOLUSDT / Bollinger | 4h | 640 | CONFIRM_CLOSE_NET_RR2 | 2 | +3.003645 | +2.784285 |

**Não há base para promoção.** As confirmações e os alvos ajustados produziram escolhas favoráveis no treino, mas predominam perdas nas janelas seguintes. Isso não autoriza novas tentativas de ajuste no mesmo período até surgir um backtest bonito. Nenhum desses números constitui lucro validado.

## Validação futura preparada e ainda pendente

O protocolo separa uma janela posterior ao congelamento a partir de **02/11/2026 às 08h00 MT (12:00 UTC)**. O código, o protocolo, o resultado do treino antigo e suas escolhas são fixados por hashes antes desse horário. Nenhum candle anterior é aceito como nova validação. Sinais originados antes do início também não podem ser carregados para dentro da janela como entradas confirmadas.

Para a escolha futura, usa-se apenas o treino de todo o snapshot antigo. O algoritmo predefinido escolheu três candidatas para observação — **não para operar**:
- ETHUSDT / Bollinger / 4h: `CONFIRM_CLOSE_NET_RR2`.
- SOLUSDT / Donchian / 1h: `CONFIRM_CLOSE`.
- SOLUSDT / Bollinger / 4h: `CONFIRM_CLOSE_NET_RR2`.

Nenhuma regra é escolhida ou recalibrada olhando a janela futura. Se o treino antigo manda abster-se, um resultado futuro favorável não pode criar retroativamente uma candidata. Alterar código, protocolo ou arquivo de treino após o congelamento faz o avaliador recusar o teste.

O mínimo é de 96 barras novas completas e 30 operações encerradas por braço comparado, incluindo estresse. Só as barras representam ao menos 24h em 15m, quatro dias em 1h e 16 dias em 4h; reunir operações suficientes pode levar mais tempo. Sobreviver à auditoria S1 não supre essa evidência financeira.

Uma coleta histórica posterior ao congelamento continua sendo replay com disponibilidade e execução hipotéticas. Ela não substitui a coleta prospectiva real S3, recibos em tempo real ou fills da corretora. A política e o diário S3 existentes permanecem congelados e intocados. Nenhuma rotina recorrente ou captura automática nova foi criada.

O avaliador recusa snapshots que perderam o começo da janela, timestamps incompatíveis e substituição por uma janela posterior mais conveniente. Nenhuma condição do avaliador autoriza ordens ou promoção automática.

## Reprodução

```sh
cd backend
python -m pytest -q app/tests/test_gate_entry_exit_research.py
python -m scripts.gate_entry_exit_experiment \
  --output /tmp/entry-exit-replay.json.gz \
  --verify ../docs/research/gate-s3i-20261102/results.json.gz
python -m scripts.gate_entry_exit_experiment \
  --future-snapshot CAMINHO_DO_SNAPSHOT_POSTERIOR.json.gz \
  --output /tmp/entry-exit-holdout.json.gz
```

O snapshot futuro deve usar o formato normalizado e o checksum do capturador isolado já existente, obtido somente por GET público. O arquivo antigo retorna `WAITING_FOR_POST_FREEZE_DATA`; esse retorno não é lucro nem validação concluída. A conferência de hashes usa `freeze.json` e não muda os parâmetros.

Verificação local: 70 testes direcionados aprovados, incluindo 21 novos. Incluem causalidade da confirmação, expiração, invalidação por stop, equação do alvo líquido, paridade dos controles originais/S1, seleção real invariante a mudanças posteriores, rejeição de dados antigos como holdout, integridade do código/treino e proibição de refazer seleção no holdout. A reprodução em GitHub e os testes gerais são registrados no PR ao concluir.
