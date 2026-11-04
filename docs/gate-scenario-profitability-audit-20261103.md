# Auditoria de lucratividade do observador de cenários

**Resultado: inconclusivo, sem avaliação financeira.** Não existem neste estudo
operações de mercado encerradas do novo observador cuja lucratividade possa
ser medida. Não declarar lucro zero, taxa de acerto zero ou estratégia
definitivamente não lucrativa. Métricas financeiras permanecem NOT_COMPUTED/null.

## Objeto e limites

Auditado o observador do PR #99, commit
`ec1917a99413119ed009ca307d80ec5ad60bc94e`. Seu SHA256 de implementação é
`5ca00189023c4b8122577da91f8f41e7b8fc2c06fdc48c6131c63772615c3876`.
O auditor recusa outra implementação. O observador confirma uma sequência e
produz CANDIDATE_ONLY; ainda não calcula quantidade/lotes, preenchimentos,
posição, stop/alvo executados, duração máxima da posição, pagamentos de funding
ou liquidação/margem. Seu sinal técnico não é uma operação encerrada.

Os seis exemplos anteriores eram sintéticos e verificavam software. As duas
propostas nesses exemplos não são duas operações de mercado. Os 467 testes do
PR anterior não são evidência de rentabilidade.

## Fonte histórica independente do serviço implantado

A intenção de coleta foi declarada em `2026-11-04T00:47:50.951126Z`
(03/11/2026, 20:47:50 UTC−4), antes dos cinco GETs públicos. Mercado fixado:
BTC_USDT. Fonte de preços: arquivos mensais oficiais candlesticks_1m de novembro
e dezembro de 2023. Janela de fonte pontuada: 01/11–15/12/2023, fim exclusivo.
Warmup planejado: 01/11–15/11; avaliação pretendida: 15/11–15/12.

Dois arquivos de funding_applies e uma resposta pública de mark 4h completaram
o orçamento de cinco consultas, sem retry. Os arquivos brutos, recibos, hashes
e horários foram preservados. Os campos históricos de preços são t, volume,
close, high, low, open. O timestamp t é de abertura; a leitura S1 precisaria do
fechamento da agregação e de um receipt admissível. A fonte usa volume de
contratos, sem alegar conversão histórica para BTC.

A janela foi escolhida antes da coleta para ficar anterior às séries principais
de 4h de 2024 em diante. Funding de 2023 já foi pesquisado em outro estudo; não
há certificação de histórico globalmente intocado. A reserva existente não foi
aberta ou avaliada financeiramente. Receber arquivos antigos hoje não constitui
evidência prospectiva. Linhas de dezembro posteriores ao fim fixado só passaram
pela validação de integridade do CSV e ficaram fora da janela analisada.

## Achados de integridade e disponibilidade

| Item | Resultado |
|---|---:|
| Minutos esperados em 01/11–15/12 | 63.360 |
| Minutos presentes nessa janela | 63.291 |
| Minutos ausentes | 69 |
| Minutos ausentes na avaliação pretendida de 30 dias | 44 |
| Duplicatas | 0 |
| Cobertura da janela completa | 99,891098% |
| Taxas liquidadas de funding no intervalo inclusivo | 133 |
| Candles mark 4h, incluindo apoio na fronteira final | 266 |

Ausência no arquivo não comprova indisponibilidade da corretora nem ausência de
negociação. Sua causa não foi verificada. Nenhum minuto foi preenchido com zero,
forward-fill ou preço inventado, e nenhum período foi trocado para melhorar o
resultado. As lacunas inteiras estão no resultado reproduzível.

Agregando somente blocos UTC com todos os minutos presentes:

| Período | Barras completas | Maior sequência contínua |
|---|---:|---:|
| 15m | 4.167 | 705 |
| 1h | 1.012 | 176 |
| 4h | 231 | 43 |

O S1 exige ao menos 80 candles contínuos em cada período. Derivando todo o
contexto exclusivamente desses arquivos de 1m, nenhuma das 2.880 fronteiras
de 15m na avaliação possui os três contextos completos. Isso é uma limitação
dessa construção de dados, não prova de que a Gate não tenha candles válidos
de 4h ou de que o observador nunca possa operar. Candles oficiais independentes
de 15m/1h/4h poderiam reduzir essa limitação; não foram baixados neste orçamento.
Eles não reconstituiriam automaticamente os minutos ausentes necessários para
reconfirmação e execução.

## Por que não há PnL

Há dois bloqueios independentes: o ciclo financeiro da nova regra não está
implementado/congelado e a fonte capturada falha na continuidade exigida. Não
foi executado replay financeiro, seleção de estratégias, bootstrap ou cálculo de
significância. Não foram apresentados os resultados de regras antigas como se
pertencessem a este observador. Dados faltantes não foram transformados em
operações vencedoras nem em perdas artificiais.

O observador considera 5 bp de tarifa e 6 bp de slippage por lado em seu filtro
de espaço. Essas são premissas de planejamento; a tarifa pessoal não foi
consultada e funding ainda não entra nesse filtro. Mesmo uma razão projetada
de ganho/perda >=2 não informa a taxa de acerto ou o retorno efetivo da regra.

## Correção necessária antes de medir retorno

1. Congelar o contrato financeiro: entrada estritamente posterior à informação
   disponível, tratamento de gaps, stop/alvo ambíguos, prazo de saída, quantidade,
   limites de risco e funding. A implementação precisa permanecer separada do
   OMS e passar por contas financeiras independentes.
2. Definir e verificar uma fonte de contexto 15m/1h/4h e a continuidade de 1m.
   Estabelecer antes do replay o que acontece com cenário/posição em cada lacuna;
   não eliminar posteriormente trades inconvenientes.
3. Só então registrar uma avaliação histórica sob custos e stress, preservando
   falhas e todas as tentativas. Uma eventual candidata precisará de evidência
   nova suficiente antes de promoção ou execução.

Não iniciar outra família de estratégias para contornar esses bloqueios. A
auditoria consolidada da pesquisa S4 continua um trabalho separado.

## Verificação e reprodução

Foram conferidos os cinco recibos e os dez hashes de arquivo/recibo. Uma conta
independente reconstruiu timestamps, OHLCV, 69/44 minutos ausentes e as sequências
de agregados usando contagem por bin, sem chamar o agregador do auditor.

483 testes locais passaram em 53,22 s, incluindo 16 novos. O resumo teve um
aviso de dependência e houve um aviso inicial de depreciação do pytest-asyncio.
Os testes incluem troca de campos, OHLC inválido, NaN, duplicidade, timestamp,
gaps, mês errado, corrupção de recibo e a proibição de tratar dados completos
como prova de lucro. Nenhuma mudança no observador congelado, S3, OMS, risco,
serviço implantado, volumes ou credenciais foi realizada.

```bash
cd backend
python -m scripts.gate_scenario_profitability_audit --root /analysis/evidencias --output /analysis/reproducao.json
```

O pacote contém intenção, respostas brutas, recibos, auditoria, conferência
independente e hashes. O código fica no repositório. O auditor é somente leitura
e não faz novos downloads; a saída deve ser um caminho novo.

Fontes primárias: [Gate: arquivos históricos](https://www.gate.com/developer/historical_quotes),
[Gate: API de futuros](https://www.gate.com/docs/developers/apiv4/en/futures/) e
[Gate: tabela publicada de tarifas](https://www.gate.com/pt-br/announcements/article/50390).
