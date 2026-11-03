# S4 — dados reais públicos, diagnóstico e revisões com teste temporal separado

## Escopo

Entrega de pesquisa isolada sobre o código da PR #89. Nenhum arquivo existente
de runtime é alterado. S3, OMS, risco operacional, variáveis, credenciais,
volumes, agentes registrados e serviço implantado permanecem fora deste fluxo.
Não há merge, deploy, ordens, coleta recorrente ou novo serviço pago.

## 1. Fonte real, somente leitura

`gate_s4_real_source.py` usa apenas GETs nos endpoints públicos de contratos e
candles de `https://api.gateio.ws/api/v4/futures/usdt/`. O host é fixo, os caminhos
são permitidos explicitamente e redirecionamentos são recusados. Não há acesso
a contas ou a endpoints de ordens. Ler preços da produção não habilita LIVE.
O adaptador Testnet usado pelo S3 não é alterado nem recebe fallback de fonte.

Há 6.000 candles por BTC/ETH/SOL × 15m/1h/4h, páginas de até 500, três workers,
limites de bytes e validação de continuidade/OHLCV/metadados/relógios. Os dados
reais da captura de 03/11 são válidos nas nove séries: **54.000 candles**. Páginas
originais, URLs públicas, recebimentos atuais em lote, hashes e conversão de
volume por multiplicador de contrato são preservados. Isso não é recibo
prospectivo do candle nem prova de fill executável.

O fechamento da janela é o mesmo anchor da captura anterior Testnet. No intervalo
ETH 4h que falhou antes, a fonte real informa open=3282.92, high=3283.84, low=3177
e close=3210.51, um OHLC válido. O registro Testnet tinha low=3282 e close=3210.5,
que violava a mínima. Nenhuma fonte foi corrigida ou sobrescrita.

## 2. Diagnóstico dos primeiros 60%

Somente `[220,60%)` é usado para diagnóstico financeiro de três matrizes por
série. O início em 220 dá contexto para EMA200 e usa a mesma janela entre
estratégias. Os indicadores/grupos não recebem candles posteriores ao corte.
O diagnóstico separa bruto reconstruído antes de slippage, arrasto de slippage,
comissões, funding modelado e líquido; conta saídas por stop, alvo e tempo, além
de grupos de contexto disponíveis na decisão.

Na amostra de treino: **15/27 casos negativos antes dos custos; outros 9/27
passam de bruto positivo a líquido negativo; somente 3/27 ficam positivos
após custos**. Essa atribuição reconstrói custos no mesmo caminho de trades
salvos, não é um replay alternativo sem taxas e não prova causalidade isolada.
As janelas/contas são independentes e não se soma esse PnL como carteira.

Os agrupamentos não sustentaram o uso imediato de simples confirmação EMA200
ou baixa eficiência direcional como correção. Essas propostas preliminares
foram descartadas antes de replay de validação; o artefato conserva sua lista.
O diagnóstico v1 também foi preservado; v2 corrige apenas pareamento de horários
`Z`/`+00:00` na comparação de fontes. As métricas econômicas de treino são
idênticas entre v1 e v2. Nenhuma mudança ocorreu após abrir a validação.

## 3. Três hipóteses delimitadas

Uma revisão por matriz testa a mesma restrição econômica explícita: só aceitar
uma proposta se o alvo cobrir pelo menos três orçamentos conservadores de custo.
Cada orçamento assume comissão de 12 bps e slippage de 12 bps por lado, mais
2 bps de funding: 50 bps. Portanto, o alvo mínimo é 150 bps (1,5% do fechamento
disponível na decisão). A regra é fixa entre cenários e não reduz custos ou
altera stop, alvo da matriz, tamanho de posição ou limites de risco.

Essa restrição pode reduzir operações com alvo pequeno em relação ao custo;
não se afirma que ela corrige uma expectativa bruta negativa. Não houve busca
em grade de limiares nem escolha do melhor valor após validação. As três regras
são propostas humanas explícitas, não geração autônoma por IA.

Mantêm-se também as seis candidatas anteriores como controles registrados na
nova fonte e janela. O universo é de **9 regras × 9 séries = 81 tentativas** e
**486 testes** de candidata/cenário/benchmark. As 54 tentativas Testnet anteriores
permanecem no artefato original e são referenciadas por hash, não apagadas.
Não se afirma correção de todas as buscas históricas do projeto.

## 4. Validação posterior ao diagnóstico

Para 6.000 barras, o diagnóstico termina antes do índice 3600; as duas janelas
de validação são `[3625,4200)` e `[4225,4800)`, com 25 barras de separação. Os
últimos 20%, `[4800,6000)`, não são avaliados financeiramente nesta rodada.

**Atenção temporal:** as datas históricas podem se sobrepor a pesquisas anteriores
em Testnet. Esta separação impede usar o resultado da validação desta rodada
para formular suas regras, mas não transforma essas datas em um holdout global
nunca visto pelo projeto. Parte dos 20% finais novos coincide com validação
Testnet anterior; a faixa final de 600 candles originalmente reservada continua
sem replay financeiro. Evidência prospectiva nova permanece obrigatória.

Cada candidata e a matriz pareada recebem custos nominais, custos dobrados e
custos dobrados com atraso de um candle. O patrimônio inclui estimativa líquida
de saída das posições abertas e marcas no fim da janela; estas ficam fora da
contagem separada de trades encerrados. Não há marca de liquidação ou DD intrabar.
Comissões/slippage/funding são premissas de pesquisa, não custos pessoais
confirmados da conta do usuário. A fonte real não elimina essa limitação.

O diagnóstico estatístico reutiliza o método condicionado da PR #89: bootstrap
estacionário por janela com blocos 8/16/32, 9.999 replicações, seed fixa,
sensibilidade menos favorável e Holm sobre a família completa. As premissas de
estacionariedade/dependência fraca não foram verificadas independentemente.
Nenhum resultado é apresentado como prova de lucro ou autorização de operação.

## Implementação e provas de isolamento

O leitor de regras `CausalParentPlans` usa exatamente a recorrência EMA do S2
em prefixos, validando dados uma vez e evitando refazer toda a história a cada
barra. Testes comparam planos completos em 15m/1h/4h e trades de replay inteiros
contra o avaliador original inalterado. Testes também cobrem alteração de dados
futuros, abstinência de regras, orçamento de custos com oráculo independente,
conservação de 81 tentativas falhas, hashes e recusa de endpoints privados.

O diário é criado exclusivamente, com triggers contra update/delete, transações,
cadeia de hashes e implementação/dados congelados. Falhas/interrupções são
preservadas, sem retry/reset automático. Hashes não atestam contra um
administrador capaz de reescrever tudo. Arquivos completos são lidos para
qualidade/hash; reserva financeira não significa ocultação física do arquivo.

## Uso sob demanda

```bash
cd backend
python -m scripts.gate_s4_real_revisions capture --output /analysis/real/history --anchor 1793714117
python -m scripts.gate_s4_real_revisions diagnose --catalog /analysis/real/history/catalog.json \
  --prior-root /analysis/prior-testnet --output /analysis/real/training-diagnosis.json
python -m scripts.gate_s4_real_revisions register --root /analysis/real/study \
  --catalog /analysis/real/history/catalog.json --diagnosis /analysis/real/training-diagnosis.json \
  --output /analysis/real/manifest.json
python -m scripts.gate_s4_real_revisions run --root /analysis/real/study --output /analysis/real/review.json
```

Os caminhos são exemplos. A reprodução deve usar outro diretório e preservar
o diário original; ela não é nova evidência independente. `--source-root`
permite relocalizar dados arquivados sem mudar hashes. Um relatório de uma
suíte existente verifica o diário e recalcula estatística, sem reexecutar trades.

Contrato oficial de dados públicos: https://www.gate.com/docs/developers/apiv4/en/futures/
Método estatístico e fontes primárias: `gate-s4-historical-statistical-validation-20261103.md`.
