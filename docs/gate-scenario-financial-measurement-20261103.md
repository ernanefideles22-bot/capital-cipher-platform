# Medição financeira da leitura por cenários

**O replay financeiro foi executado. A regra não abriu operações nessa amostra.**
No cenário nominal dos dois blocos: 145 planos, 53 toques, 28 rejeições, um
rompimento e zero entradas. O único rompimento foi cancelado na reconfirmação
porque o alvo não oferecia espaço após os custos. Não houve seleção ou ajuste
de parâmetros depois desse resultado.

## Contrato e fonte

Permanece inalterado o observador do PR #99. O novo módulo é um invólucro de
pesquisa econômica, sem broker, OMS, risco operacional, cadastro de agentes ou
deploy. Ele usa a sequência original de contexto 4h/1h, níveis de 15m, toque,
rejeição, rompimento e reconfirmação de 1 minuto. Cada período usa somente os
últimos 80 candles fechados, com pivôs confirmados causalmente.

Janela fixa: BTC_USDT de 15/11 a 15/12/2023, fim exclusivo, dividida em dois
blocos de 15 dias. Warmup desde 01/11. Os dados anteriores de 1m, funding e mark
foram reutilizados, preservando suas 69 lacunas de 1m (44 na avaliação). Seis
GETs públicos adicionais, declarados antes da coleta, forneceram arquivos
oficiais independentes de 5m, 1h e 4h para novembro/dezembro. Os três grids estão
completos na janela; o OHLC de 1h/4h coincide exatamente com a agregação dos 5m.
As barras de 15m são agregações completas de três barras oficiais de 5m.

A falta de um minuto cancela o cenário e a entrada ainda não executada; não é
preenchida. Uma posição já aberta teria stop/alvo modelados como brackets
permanentes sobre o OHLC completo de 5m, mesmo durante falha da alimentação de
1m. Isso é uma hipótese explícita de execução, não prova de ordens na corretora.

O código e as regras finais foram congelados no registro em
`2026-11-04T01:18:30.617286Z` (03/11, 21:18:30 UTC−4), antes do replay financeiro.
A coleta precedeu esse registro final; as intenções de fonte precederam seus
downloads. Esta é uma hipótese derivada posteriormente às pesquisas anteriores,
não confirmação independente/globalmente intocada. A reserva existente não foi
avaliada.

## Execução e contabilidade congeladas

- Entrada na primeira abertura de 5m estritamente posterior à disponibilidade
  da proposta. Uma observação de 1m fechada em T fica disponível em T+2s; só
  aberturas posteriores podem usar essa informação. Revalidações pendentes
  usam apenas informações disponíveis antes dessa abertura.
- Stop e alvo mantêm níveis estruturais congelados. Stop prevalece se ambos
  aparecerem no mesmo OHLC. Gap adverso de stop usa a abertura pior; gap
  favorável de alvo usa o preço do alvo. Não há preço retroativo na marcação.
- Saída máxima por tempo: seis horas, na abertura seguinte correspondente.
  Intervalo de uma hora após saída; sem posições sobrepostas ou reentrada
  dentro da mesma barra. Posição ainda aberta no fim teria saída estimada,
  separada da amostra de operações encerradas.
- Capital de referência: 1.000 USDT por bloco/cenário. Risco planejado de 0,25%
  por operação, calculado incluindo taxa/slippage no stop; notional <=capital.
  Bloqueios de novas entradas por perda diária realizada de 2% e drawdown de
  pesquisa de 10%. O risco central implantado não é alterado.
- Quantidade arredondada para baixo em lotes hipotéticos de 0,0001 BTC. Não há
  reconstrução de regras históricas de lote, margem, tiers ou liquidação. Este
  referencial não certifica execução com capital real de 50 USDT.
- Taxa planejada de taker de 5 bp por lado, não consultada na conta pessoal.
  Slippage nominal de 6 bp por lado; stress de 12 bp; terceiro cenário adiciona
  atraso de mais uma barra de 5m. O filtro de ganho/perda após esses custos
  permanece >=2. Não se presume maker, GT, rebate ou juros de caixa.
- Funding usa taxas efetivamente liquidadas, com sinal correto, quantidade fixa
  e mark de abertura de 4h. Extremos dos candles adjacentes dão limites
  condicionais de pagamento. Eventos coincidentes com entrada/saída na abertura
  têm ponto zero e limites incluindo zero e pagamento integral. Não são ticks
  exatos ou intervalos de confiança. Nenhuma posição foi aberta nesta amostra,
  portanto nenhum funding ou taxa de operação foi lançado.

## Resultado financeiro

| Bloco | Cenário | Operações encerradas | Líquido USDT | Taxa de acerto | Fator de lucro |
|---|---|---:|---:|---|---|
| 15/11–30/11 | Nominal | 0 | 0 | Indefinida | Indefinido |
| 15/11–30/11 | Stress | 0 | 0 | Indefinida | Indefinido |
| 15/11–30/11 | Stress + atraso | 0 | 0 | Indefinida | Indefinido |
| 30/11–15/12 | Nominal | 0 | 0 | Indefinida | Indefinido |
| 30/11–15/12 | Stress | 0 | 0 | Indefinida | Indefinido |
| 30/11–15/12 | Stress + atraso | 0 | 0 | Indefinida | Indefinido |

Capital permaneceu em caixa sob a hipótese de ausência de remuneração. Não
apresentar zero operações como estratégia lucrativa, taxa de acerto de zero ou
resistência comprovada a custos/atrasos. Esses cenários reaproveitam os mesmos
dois períodos, não seis amostras independentes. Custos de desenvolvimento/IA e
infraestrutura não entram neste PnL de negociação.

O critério registrado exigia >=30 operações encerradas em cada referência,
líquidos fechado e marcado inferiores positivos, fator de lucro >=1,2,
participação da maior vitória <=50% e drawdown de fechamento <=5%. Todos os
casos falharam por ausência de operações/ganhos. Não houve bootstrap,
significância, promoção para S5 ou autorização de execução.

## Funil nominal e causa observada

| Etapa | Primeiro bloco | Segundo bloco | Total nominal |
|---|---:|---:|---:|
| Planos congelados | 77 | 68 | 145 |
| Toques na região | 27 | 26 | 53 |
| Rejeições confirmadas | 11 | 17 | 28 |
| Rompimentos confirmados | 0 | 1 | 1 |
| Entradas propostas após reconfirmação | 0 | 0 | 0 |

Não somar esse funil novamente nos cenários de stress. Dezesseis cenários ativos
nominais foram cancelados em lacunas de 1m; outros expiraram ou foram
invalidados. Essas quantidades não são operações perdedoras.

O único rompimento chegou à reconfirmação em 06/12/2023 às 09:46:02 UTC:
compra de referência 43.848,40, alvo 43.865,00, stop 43.676,9364. A distância
favorável era 16,60 no preço do BTC, contra 171,46 até o stop: razão bruta de
aproximadamente 0,0968. Após taxa/slippage planejados, o ganho projetado por
BTC seria -79,8847, portanto a regra cancelou corretamente essa proposta. Esses
números são por BTC de referência, não uma perda efetivamente realizada.

O achado é específico: esta combinação de níveis, sequência e filtros não
produziu entradas negociáveis nessa amostra. Não demonstra inviabilidade de
toda análise técnica, nem justifica remover filtros/afastar alvos depois de
ver o resultado. Uma revisão da montagem de níveis seria uma nova versão e
precisaria de protocolo e confirmação separados.

## Conferência e reprodução

O diário SQLite registra REGISTERED -> STARTED -> RESULT, com hashes encadeados,
fonte/política congeladas e recusa de retry/reset. A cadeia final é
`e3e5f1f0489297a323e1d3547bc1777a0dcae3e51c4de46f638aff20874ee48c`.
Hash das séries normalizadas:
`90ba1faa6e93019cbbf3664ac54ddffcb1185746b31f5d7061949195f954bfe5`.

Testes verificam sizing, lote, custos, funding positivo/negativo, limites de
pagamento, stop/alvo simultâneos, gap, relógio sem futuro, agregação UTC, falhas
do registro e um candidato sintético que efetivamente entra e fecha no alvo no
invólucro financeiro. Esse caso sintético não entra no resultado histórico.
Uma conferência independente reconciliou o funil, zero fills/custos/PnL, hashes
dos dados e a aritmética Decimal do único caso rejeitado. Os testes financeiros
novos somam 19; as demais regressões são registradas no pacote de evidências.

```bash
cd backend
python -m scripts.gate_scenario_financial_measurement review --root /analysis/evidencias --output /analysis/leitura-nova.json
```

## Limitação de preservação identificada na retomada em 04/11/2026

O workspace temporário foi reinicializado antes da publicação do pacote de
evidências desta medição. O código e este registro de resultados foram
recuperados de objetos Git já enviados. Não foram recuperados o diário SQLite,
os arquivos brutos suplementares, os eventos completos nem as saídas locais de
teste. Os hashes acima são os valores registrados anteriormente, não uma
reverificação dos arquivos nesta retomada.

Portanto, não há ZIP completo desta execução disponível. A medição histórica
relatada permanece zero entradas/zero PnL, mas sua trilha bruta está incompleta
e não deve ser apresentada como auditoria financeira integralmente verificável.
Os 96 testes direcionados (19 novos) são o resultado local anteriormente
registrado; as verificações de CI do PR constituem evidência de código separada.
Nenhum estudo ou diário foi recriado para aparentar continuidade. Uma eventual
reprodução com dados recuperados deve ser identificada como reprodução, nunca
como nova evidência independente de lucratividade.
