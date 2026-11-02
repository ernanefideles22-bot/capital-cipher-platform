# Capital Cipher — teste de viabilidade após custos (S3H)

**Conclusão: nenhum agente aprovado por lucratividade.** O filtro de custos reduziu perdas em parte das comparações, mas não demonstrou vantagem incremental nas janelas cronológicas seguintes ao treino. A coleta da S1, o Railway, o OMS, o motor de risco e a política prospectiva S3 permanecem inalterados.

## Protocolo fixado antes desta execução

Protocolo `GATE_COST_VIABILITY_RESEARCH_V1`, registrado em 02/11/2026 às 05:55:53 UTC, após a captura e antes de executar estas hipóteses. Os limiares 2x e 4x não foram alterados depois de observar os resultados. SHA-256 do protocolo: `64679aceab27dde4a6e78b35f65a4bc571e03d6b73d0afc310c82c6d120a8de7`.

Entrada: snapshot normalizado de candles públicos da **Gate Testnet**, já arquivado no PR #84. SHA-256 `02c406b670f8e802c2b48df1d1275f92b1a0683d8566d455e21ca4aa7be96eb5`. Todo o histórico já havia sido observado; nenhum resultado deste relatório é uma validação em amostra intocada. Os 800 candles de 15m, 1h e 4h cobrem durações diferentes: comparar os totais entre timeframes não é um ranking em período comum.

- BTC, ETH e SOL; Donchian, EMA e Bollinger; 15m, 1h e 4h: **27 combinações**.
- Controles: estratégia original e caixa. Confluência S1 adicional apenas em 15m, onde existe o adaptador congelado. Não inventamos confluência para entradas 1h/4h.
- Hipóteses fixas: alvo >= 2x ou 4x o custo estimado de ida e volta, com e sem S1 quando disponível. A estimativa usa somente o fechamento conhecido; não acessa abertura seguinte ou retornos posteriores.
- Estimativa de custo por unidade = último fechamento × [2 × (taxa + slippage por lado) + funding × intervalos do prazo máximo de permanência] / 10.000. Funding considera o prazo máximo já definido para a estratégia. É uma aproximação conservadora, não lucro esperado, probabilidade de acerto ou garantia de execução.
- Cenário base: 6 bps de taxa e 6 bps de slippage por lado, mais funding modelado de 1 bp/8h. Estresse: taxa igual, slippage 12 bps por lado e funding 2 bps/8h. O filtro continua usando a estimativa base no estresse; não é recalibrado.
- Stops, alvos, prazo máximo, limites de risco e dimensionamento são os mesmos da estratégia original: conta hipotética de US$ 1.000, risco de 0,25% por entrada e limite de exposição de 1x. Cada comparação usa conta independente; os resultados não são de uma carteira.
- Cada variante é simulada integralmente: mudanças na ocupação, capital, limites e oportunidades são recalculadas. Não filtramos retrospectivamente uma lista de trades pronta nem apenas subtraímos custos.

## Seleção cronológica e proteção contra conclusões falsas

Três janelas por combinação, com treino expansivo terminando nos índices exclusivos 400, 520 e 640, seguido por 120 barras de avaliação. As decisões da avaliação começam no índice do fim do treino, com entrada na abertura seguinte segundo o replay histórico existente. Cada janela reinicia uma conta independente; posições não atravessam a fronteira.

Escolha feita somente no treino: no mínimo cinco operações encerradas, lucro líquido encerrado positivo, fator de lucro > 1 (ou todas as operações positivas) e resultado superior aos controles disponíveis e ao caixa. Entre elegíveis, escolhe o maior líquido encerrado; empate pelo identificador da variante. Sem elegível, não opera. Marcação forçada no fim da janela não conta como operação encerrada nem como lucro na seleção.

O rastreamento registra essas marcações separadamente. A curva de capital completa ainda as inclui onde existirem; a coluna de líquido usada abaixo as exclui. O limite mínimo para evidência descritiva foi 30 operações encerradas em cada braço comparado, inclusive no estresse. Esse mínimo é uma condição necessária do protocolo, não prova estatística de rentabilidade. Mesmo se houvesse uma janela positiva, ainda faltariam avaliação futura, tratamento das múltiplas hipóteses e execução realista.

## Resultado das 81 janelas

| Indicador | Resultado |
| --- | ---: |
| Combinações avaliadas | 27 |
| Janelas cronológicas | 81 |
| Janelas que selecionaram uma variante no treino | 5 |
| Janelas sem variante elegível no treino | 76 |
| Janelas com operações suficientes em todos os braços | 0 |
| Janelas aprovadas no filtro descritivo | 0 |
| Agentes autorizados para operar | 0 |

As cinco escolhas no treino foram `COST_4X`. Nas quatro janelas em que a variante escolhida operou, os trades e os resultados foram os mesmos do controle original nessa janela. Na quinta, SOL/EMA 15m, ela não operou. Portanto, os ganhos de algumas comparações históricas fixas não se traduziram em vantagem incremental demonstrada após a seleção cronológica.

| Mercado / estratégia | Timeframe | Fim do treino | Trades seguintes | Líquido base USD | Líquido estresse USD |
| --- | --- | ---: | ---: | ---: | ---: |
| ETHUSDT / Bollinger | 4h | 520 | 2 | -5.448350 | -5.654636 |
| SOLUSDT / EMA | 15m | 640 | 0 | +0.000000 | +0.000000 |
| SOLUSDT / Bollinger | 1h | 400 | 3 | +2.046790 | +1.416648 |
| SOLUSDT / Bollinger | 1h | 520 | 3 | +0.369583 | -0.254530 |
| SOLUSDT / Bollinger | 1h | 640 | 5 | +3.871084 | +2.989205 |

SOL/Bollinger 1h chegou a +0,369583 USD em uma janela, mas passou a -0,254530 USD no estresse, com apenas três operações. Isso mostra fragilidade a custos. As três janelas desse caso somam só 11 operações e não demonstram ganho do filtro sobre o controle; não são uma curva contínua de capital.

## Comparações fixas: últimos 40% do histórico

Esta tabela é diagnóstica, sem escolha pelo resultado mostrado. Todos os valores são líquidos das operações encerradas; marcações de fronteira ficam no arquivo de evidência. `N` é o número de operações encerradas. `4x estresse` mantém a mesma hipótese e refaz a simulação com os custos maiores.

| Mercado | TF | Estratégia | Original USD (N) | 2x USD (N) | 4x USD (N) | 4x estresse USD (N) |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| BTCUSDT | 15m | Donchian | -13.424401 (6) | -5.584799 (4) | +2.610379 (2) | +2.105369 (2) |
| BTCUSDT | 15m | EMA | -22.704740 (11) | -3.757512 (4) | +0.905691 (1) | +0.483625 (1) |
| BTCUSDT | 15m | Bollinger | -36.931853 (19) | -10.954823 (3) | +0.000000 (0) | +0.000000 (0) |
| BTCUSDT | 1h | Donchian | -10.643782 (6) | -10.643782 (6) | -10.643782 (6) | -11.801290 (6) |
| BTCUSDT | 1h | EMA | -15.263749 (9) | -15.263749 (9) | -12.051892 (6) | -13.776172 (6) |
| BTCUSDT | 1h | Bollinger | -8.197412 (17) | -0.418937 (15) | -2.047847 (10) | -4.509441 (10) |
| BTCUSDT | 4h | Donchian | -3.542686 (5) | -3.542686 (5) | -3.542686 (5) | -6.768069 (5) |
| BTCUSDT | 4h | EMA | -5.566208 (8) | -5.566208 (8) | -5.566208 (8) | -9.657853 (9) |
| BTCUSDT | 4h | Bollinger | -10.729694 (13) | -10.729694 (13) | -10.729694 (13) | -12.888744 (13) |
| ETHUSDT | 15m | Donchian | -15.369375 (5) | -15.369375 (5) | -2.775597 (1) | -2.877337 (1) |
| ETHUSDT | 15m | EMA | -28.108808 (12) | -21.103433 (7) | -12.595779 (4) | -13.564329 (4) |
| ETHUSDT | 15m | Bollinger | -20.230522 (18) | -7.628708 (4) | +3.063289 (1) | +2.809220 (1) |
| ETHUSDT | 1h | Donchian | -6.239064 (6) | -6.239064 (6) | -6.239064 (6) | -7.132888 (6) |
| ETHUSDT | 1h | EMA | -1.057232 (10) | -1.057232 (10) | +2.342180 (9) | +0.395053 (9) |
| ETHUSDT | 1h | Bollinger | -2.758245 (14) | -2.758245 (14) | +1.359968 (10) | -0.402812 (10) |
| ETHUSDT | 4h | Donchian | -1.088445 (6) | -1.088445 (6) | -1.088445 (6) | -1.536180 (6) |
| ETHUSDT | 4h | EMA | +4.898385 (12) | +4.898385 (12) | +4.898385 (12) | +3.613619 (12) |
| ETHUSDT | 4h | Bollinger | -0.605282 (6) | -0.605282 (6) | -0.605282 (6) | -1.149024 (6) |
| SOLUSDT | 15m | Donchian | -7.326580 (7) | -7.326580 (7) | -11.092464 (6) | -12.411979 (6) |
| SOLUSDT | 15m | EMA | -14.710015 (12) | -14.710015 (12) | -2.268638 (3) | -3.095143 (3) |
| SOLUSDT | 15m | Bollinger | -17.764632 (19) | -19.610260 (13) | -6.251242 (2) | -6.714388 (2) |
| SOLUSDT | 1h | Donchian | -12.349601 (7) | -12.349601 (7) | -12.349601 (7) | -13.245925 (7) |
| SOLUSDT | 1h | EMA | +5.770824 (8) | +5.770824 (8) | +5.770824 (8) | +3.991724 (8) |
| SOLUSDT | 1h | Bollinger | +7.633599 (9) | +7.633599 (9) | +7.633599 (9) | +5.983450 (9) |
| SOLUSDT | 4h | Donchian | +3.797251 (7) | +3.797251 (7) | +3.797251 (7) | +3.271171 (7) |
| SOLUSDT | 4h | EMA | -10.493814 (10) | -10.493814 (10) | -10.493814 (10) | -11.632246 (10) |
| SOLUSDT | 4h | Bollinger | -3.738546 (6) | -3.738546 (6) | -3.738546 (6) | -4.204269 (6) |

Resultados favoráveis e desfavoráveis precisam ser considerados juntos:

- BTC/Donchian 15m passou de -13,424401 USD (6 operações) para +2,610379 (2) com 4x; com S1 o resultado já era igual. Duas operações não estabelecem vantagem.
- ETH/EMA 1h passou de -1,057232 (10) para +2,342180 (9) com 4x; no estresse restaram +0,395053. Este resultado fixo não gerou variante elegível na seleção cronológica desse caso.
- ETH/Bollinger 1h passou de +1,359968 com 4x para -0,402812 no estresse. Escolher apenas a coluna favorável seria enganoso.
- SOL/Donchian 15m piorou de -7,326580 (7) para -11,092464 (6) com 4x. Com S1, adicionar 4x também piorou de -4,345922 para -8,123113. Um filtro de custos não prevê a qualidade direcional da entrada.
- SOL/EMA 15m reduziu perdas de -14,710015 para -2,268638 com 4x, mas continuou negativo. Reduzir perdas e ficar sem operações não equivale a criar um trader lucrativo.

## Decisão técnica

**Não promover nenhuma variante.** Os resultados justificam estudar custos por operação, mas não justificam substituir as estratégias congeladas. Apertar o filtro novamente para deixar este histórico bonito seria sobreajuste.

O próximo estudo deve testar uma hipótese nova sobre a qualidade das entradas e obter uma amostra maior, com protocolo fixado antes de dados futuros. Os sinais positivos em SOL/1h merecem observação, mas ainda não superaram o próprio controle nem a fragilidade no estresse. Não há autorização de operar, alteração da S3 ou deploy decorrente deste relatório. A auditoria de estabilidade S1 continua sendo um requisito separado.

## Reprodução e verificação

Arquivos: `docs/research/gate-s3h-20261102/protocol.json`, `results.json.gz` e checksum adjacente. O resultado guarda os trades de todos os braços fixos, métricas de todos os candidatos no treino, critérios de escolha, operações das variantes selecionadas e hashes do código. Não usa rede para obter dados de mercado.

```sh
cd backend
python -m pytest -q app/tests/test_gate_cost_viability_research.py
python -m scripts.gate_cost_viability_experiment \
  --output /tmp/gate-cost-replay.json.gz \
  --verify ../docs/research/gate-s3h-20261102/results.json.gz
```

Verificações locais: 65 testes direcionados aprovados, incluindo 22 novos. Os controles de 15m reproduzem exatamente o PR #84; os controles 1h/4h preservam o replay original sob estresse. Testes adicionais cobrem decisões sem abertura futura, seleção de uma variante real invariante a mudanças posteriores, marcações excluídas da seleção, funding por timeframe, custos maiores sem recalibração, rejeição de hipóteses não registradas e ausência de promoção mesmo em cenário sintético positivo. A validação no GitHub é registrada no PR após a execução.
