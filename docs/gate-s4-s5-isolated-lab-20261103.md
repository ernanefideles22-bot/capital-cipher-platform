# S4/S5 — laboratório isolado e seleção descritiva

## Entrega e estado

O S3 já coleta as estratégias congeladas na versão `767f9e2`. Pesquisar uma
mutação nessa mesma amostra e atribuir-lhe o histórico da estratégia original
invalidaria a medição. Esta entrega cria um laboratório **offline e sob demanda**,
com seu próprio diário, sem importar esses módulos no serviço implantado.

**S4: primeira implementação do laboratório governado; não pesquisa autônoma
operacionalmente homologada. S5: estrutura de triagem e fila de revisão; não
seleção estatisticamente validada nem operação.** Nenhum merge ou deploy é
necessário para executar o CLI local. Não há alteração em S1/S2/S3, OMS, limites
operacionais, agentes registrados, variáveis, dados persistentes ou infraestrutura.

## Contrato executável

1. `register` recebe 600–4096 candles canônicos GATE de um único mercado/timeframe
   e congela o hash dos bytes, proveniência, implementação, custos, risco e todas
   as hipóteses antes da primeira avaliação. Exige `received_at` explícito; não
   preenche lacunas nem inventa horários de recebimento.
2. Seis sementes determinísticas exercitam a infraestrutura: as três estratégias
   S2 existentes combinadas com filtro de participação ou teto de volatilidade.
   **Não são descobertas por IA.** Propostas externas, inclusive produzidas por
   agentes, podem usar o mesmo JSON declarativo; não há chamada LLM ou execução
   de código gerado. O vocabulário atual permite apenas esses filtros e limiares
   registrados. Entrada/stop/alvo/saída e risco da estratégia matriz são mantidos.
3. O orçamento é de até 12 hipóteses únicas por campanha, com padrão seis. A
   identidade ignora a redação da justificativa para impedir duplicatas disfarçadas.
   Não há inclusão posterior de candidatas, busca ilimitada ou retry automático.
4. São duas janelas de desenvolvimento: treino `[80,40%)`, validação
   `[40%+25,60%)`; treino expandido `[80,60%)`, validação `[60%+25,80%)`.
   Índices são inteiros e intervalos semiabertos. Há 25 barras de separação,
   acima do horizonte máximo de 24 barras das matrizes. A segunda janela de
   treino inclui a validação anterior, como walk-forward expandido. As propostas
   permanecem fixas e não são reajustadas com esses resultados.
5. Os últimos 20% ficam reservados e **não são avaliados** por este runner. O
   arquivo inteiro é lido/validado e identificado por hash; não se afirma
   ocultação física ou atestação externa de que ninguém já viu essa faixa.
   Cada replay recebe somente seu prefixo, sem candles futuros como features.
6. Em cada janela: treino nominal, validação nominal, validação com comissão/
   slippage/funding dobrados, estresse com sinal atrasado uma barra e controle
   matriz no mesmo período nominal. Nenhum estado ou global S2 é modificado.
   Entrada simulada segue a abertura posterior; stop tem precedência em barras
   ambíguas. Custos são premissas, não fills ou liquidez da corretora.
7. Saídas forçadas no fim de janela ficam fora das métricas de trades encerrados.
   O relatório inclui quantidade excluída; slippage já compõe os preços e não é
   subtraído novamente. DD realizado não mede drawdown intrabar. Contas/janelas
   são reiniciadas independentemente e não representam retorno de carteira.
8. O diário SQLite `s4-lab.sqlite3` usa criação exclusiva, transações de início e
   resultados, bloqueio de atualização/exclusão e cadeia de hashes. Um segundo
   worker não consome a mesma fila. Falhas são registradas no denominador;
   interrupção não autoriza recomeçar a campanha. Não há reset/limpeza automática.
   Hashes detectam alterações sem recomposição; administrador com acesso total
   pode reescrever tudo. Isso não é atestação criptográfica independente.

## S5: o que o ranking significa

A política versionada exige, **em cada janela e cenário de validação**, pelo
menos 30 encerramentos, líquido positivo contra caixa, fator de lucro líquido
de pelo menos 1,2, DD realizado de até 5% e maior ganho de até 50% da soma dos
ganhos. A validação nominal também precisa superar a matriz. Sem perdas ou sem
trades, o fator de lucro é indefinido, não infinito. Amostra pequena mantém o
estado de insuficiência, mesmo quando já há prejuízo observado.

Somente uma campanha completa, sem tentativas falhas, pode ordenar candidatas
que passaram por essa **triagem descritiva**. A ordenação usa o pior líquido de
janela/cenário, com desempate pelo ID, sem score opaco ou pooling de mercados.
O registro inclui todas as tentativas da campanha; isso **não implementa** uma
correção estatística por múltiplos testes nem agrega tentativas entre campanhas.
Como a validação de desenvolvimento participa da triagem, ela não vira holdout
independente pela mudança de nome.

Mesmo no caso favorável, o estado é `DESCRIPTIVE_REVIEW_CANDIDATE`. Permanecem
falsos `profitability_proven`, `execution_authorized` e
`automatic_promotion_authorized`. Não há conexão à promoção, alocação ou OMS.
O S3 atual não mede essas mutações: os resultados das matrizes **não podem ser
transferidos** a candidatas S4.

## Uso local

A fonte deve ser um array JSON de `Candle` com exchange `GATE`, símbolo
BTCUSDT/ETHUSDT/SOLUSDT, timeframe 15m/1h/4h, OHLCV, `closed_at` e `received_at`
com fuso. Nenhum download, uso de credenciais ou coleta é feito pelo CLI.
Os caminhos abaixo são exemplos, não arquivos existentes de mercado:

```bash
cd backend
python -m scripts.gate_s4_s5_lab register --lab /analysis/s4/campaign-001 \
  --dataset /analysis/source/gate-candles.json
python -m scripts.gate_s4_s5_lab run --lab /analysis/s4/campaign-001 \
  --dataset /analysis/source/gate-candles.json
python -m scripts.gate_s4_s5_lab report --lab /analysis/s4/campaign-001
```

`register --proposals /analysis/proposals.json --budget 6` aceita array com
`parent_strategy`, `filter_name`, `threshold`, `rationale` e `version`. O filtro
`RELATIVE_VOLUME_MIN` aceita 1.2/1.5; `ATR_PRICE_MAX` aceita 0.01/0.02. Estratégias
matrizes são as três constantes de `gate_classical_s2.STRATEGIES`. A interface
rejeita extensões não suportadas em vez de executar expressões/código.

Pastas `/gate-data`, seus aliases por symlink e pastas conhecidas do Gate são
recusadas como raiz do laboratório. Não copie um banco S3 WAL ativo para servir
de dataset; este runner exige candles canônicos e não abre o diário hospedado.
Nunca apague campanhas ruins para mostrar só a vencedora. Uma nova campanha
precisa permanecer no inventário global de tentativas, ainda a implementar.

## Próximos critérios para avançar

- Verificar continuidade e integridade da coleta S3 sem alterar suas regras.
- Reunir datasets Gate com proveniência e cobertura revisadas. Os testes desta
  entrega usam dados sintéticos e não produzem evidência de lucro de mercado.
- Implementar registro global de experimentos, correção de múltiplas hipóteses
  com método revisado e verificação independente antes de abrir holdout final.
- Congelar a candidata escolhida e aprovar uma avaliação única na faixa final;
  este CLI deliberadamente não oferece um comando de otimização no holdout.
- Preparar uma coleta prospectiva separada por candidata S4 após revisar S3.
- Completar alertas/drift/retirada, alocação, reconciliação de execução e revisão
  humana do S5. Qualquer envio de ordem exige autorização específica.

Não existe prazo de comprovação de lucratividade derivado desta implementação.
