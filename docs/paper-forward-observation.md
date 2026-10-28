# Observação contínua PAPER

A Fase B foi encerrada com PASSED no runtime `ec882651ac0a098891420d6159f0f9ba42a05f5b`.
O PR #42 foi integrado na base `test/phase-b-operational-certification-20261026`
pelo merge `11306a7e2ab0d830567ee10bd3dd8d4aa404c2c5`, sem diferença de conteúdo
em relação ao runtime certificado. Essa integração não foi em `main`.

Esta etapa observa a operação PAPER existente com dados de mercado, sem
habilitar execução LIVE/TESTNET, mudar estratégias, pesos, risco ou infraestrutura.
Também não conclui toda a Fase C do roteiro: mesas por timeframe, coordenação
de exposição e demais requisitos continuam sujeitos às suas próprias entregas.

## Coleta externa

`backend/scripts/observe_paper_forward.py` usa somente a biblioteca padrão e
GETs públicos de status, readiness, ordens PAPER, desempenho, estratégias,
orquestrador e relatórios de agentes. Não recebe credenciais nem importa o
runtime. Recusa redirecionamentos e uma origem com usuário, senha ou caminho.
Confere PAPER, LIVE indisponível e a revisão exata antes de consultar ordens.

Exemplo, com diretório privado existente e nomes novos em cada execução:

```bash
python3 backend/scripts/observe_paper_forward.py \
  --output /private/paper-forward/baseline.json

python3 backend/scripts/observe_paper_forward.py \
  --baseline /private/paper-forward/baseline.json \
  --output /private/paper-forward/capture-UTC-TIMESTAMP.json
```

O programa nunca sobrescreve um arquivo existente. Cada captura inclui hash
SHA-256 canônico e hash do próprio coletor. Preserve a baseline original e os
arquivos em armazenamento privado. **Não publique capturas de ordens ou histórico
de negociação no repositório.** Os hashes comprovam integridade dos bytes
retidos; não constituem assinatura ou autenticação das respostas do servidor.

O status `OBSERVING` significa evidência técnica utilizável, não sucesso
financeiro. Saída 2 indica coleta incompleta, escopo recusado, atenção ou janela
incomparável, preservando o JSON quando foi possível coletá-lo. Saída 0 significa
captura utilizável e, se houver baseline, comparação válida. Não redefina a
baseline para esconder uma falha. Registre o ocorrido e faça uma nova captura.

Os GETs são sequenciais, não uma transação. Divergência entre os dois relatórios
de desempenho ou entre status inicial/final exige nova coleta. Mudança de
revisão, capacidade pública, estratégias, capital inicial, reset de contadores,
janela sobreposta ou saldo não reconciliado invalida a comparação. Parâmetros
ocultos de risco/ambiente não são totalmente detectáveis por esses endpoints.

## Interpretação das métricas

- A comparação usa diferenças de contadores e PnL realizados desde a baseline.
  Uma posição aberta antes da baseline pode encerrar na janela e contribuir
  para esse resultado. Isso não é uma carteira iniciada do zero nessa data.
- O PnL líquido é o valor informado pelo motor. Taxas, slippage, spread,
  funding, impacto e liquidação permanecem nos campos individuais do relatório;
  não são subtraídos novamente do líquido. O agregado de custo publicado pelo
  runtime pode ter convenções diferentes da soma de todos os campos. Não
  redefinimos a contabilidade do motor neste observador.
- Sem novos encerramentos, a taxa de acerto da janela é nula (sem amostra).
  Lucro, perda ou acerto em uma única operação não demonstram rentabilidade.
- O breakdown completo do motor por timeframe fornece trades, acertos e PnL.
  Atribuição por estratégia é somente uma aproximação quando existe uma única
  estratégia habilitada naquele timeframe e sua configuração permanece igual.
  O histórico anterior pode ter outra configuração; não alegar causalidade.
- `/paper/orders` contém ordens abertas e apenas os últimos 100 encerramentos.
  Não use essa lista como total econômico. Os contadores completos são os de
  desempenho. Ordens abertas podem estar em status `FILLED`, com `closed_at` nulo.
- O drawdown máximo do motor pertence ao histórico realizado do runtime.
  O drawdown da campanha fica nulo: não há marcação a mercado contínua suficiente
  neste coletor para medir perdas intraperíodo das posições abertas.
- `cycle_latency_ms` é uma janela móvel de ciclos do orquestrador. Seus percentis
  não medem latência de ordem nem certificam SLO de produção. Contadores de
  falhas anteriores não demonstram sozinhos uma falha ativa.

## Agentes e critérios existentes

Os relatórios trazem os scorecards completos e candidatos observacionais.
A elegibilidade existente exige amostra mínima do próprio scorecard, acurácia
direcional acima de 50%, Brier abaixo de 0,25 e contribuição marginal positiva.
Os critérios por regime também exigem pelo menos 30 amostras históricas
classificáveis, sem olhar candles posteriores à previsão.

Amostras acumuladas podem anteceder a campanha. `evaluated_at` é a hora de
geração do scorecard, não a hora da última previsão. Histórico sem candles
causais suficientes é contado em `unavailable_historical_forecasts`, sem
preenchimento retrospectivo. Candidatura e qualificação observacional não
autorizam promoção, alteração de pesos ou execução de ordens.

## Operação do acompanhamento

Janela inicial proposta: sete dias, com uma coleta por hora. O início efetivo
depende da confirmação da tarefa agendada e de seus registros; este documento
não comprova que a tarefa já foi criada. Sete dias são uma janela operacional,
não um critério de rentabilidade ou suficiência estatística.

Preserve cada captura privada, compare com a baseline e com a captura anterior,
e registre cobertura, falhas e mudanças de amostra. Avise sobre falha de
readiness, desvio de PAPER/revisão, reset de contadores, novos encerramentos e
mudanças de candidatos/qualificação. Evite notificações sem mudança material.
Ao final da janela, apresente a cobertura e os resultados observados, inclusive
ausência de trades, dados faltantes e custos. Não force ordens para obter amostra.

Validação local do coletor:

```bash
python3 -m unittest discover -s backend/app/tests \
  -p test_paper_forward_observation.py -q
```
