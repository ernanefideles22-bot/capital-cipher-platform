# S4/S5 — validação histórica e estatística independente do S3

Esta entrega executa a validação retrospectiva das seis hipóteses declarativas
da PR #88 em BTC/ETH/SOL × 15m/1h/4h. Não altera o coletor S3, regras congeladas,
serviço Gate, OMS, variáveis, banco ou volumes. Não habilita ordens nem promove
candidatas. As sementes são filtros conhecidos, não descobertas autônomas de IA.

## Fonte e cobertura

O novo capturador usa somente GETs públicos de contratos e candles no domínio
fixo `api-testnet.gateapi.io`. O acesso HTTPS segue o proxy normal do ambiente;
nenhum adaptador ou configuração de rede implantada é alterado. Há limite de
500 barras por página, 600–4096 barras por série, nove séries e três workers.
Sem retries, endpoints privados, credenciais ou troca automática para produção.

Cada série preserva páginas JSON originais, URL pública, horários de solicitação/
recebimento, hash dos bytes e a conversão validada para candles canônicos. Volume
é convertido de contratos por `quanto_multiplier`. São observações atuais de
história em lote, não recibos prospectivos fabricados.

Na captura inicial de 03/11/2026, oito séries forneceram 3.000 candles cada.
ETHUSDT 4h foi recusado: uma barra de abertura Unix `1768780800` contém
`o=3282.9, h=3317, l=3282, c=3210.5`, isto é, fechamento abaixo da mínima.
A página foi preservada. Não houve substituição de fonte, preenchimento,
correção ou redução da janela para apagar o problema.

São dados **Gate Futures Testnet**, que não comprovam preços executáveis,
liquidez ou rentabilidade no mercado real. Timeframes têm horizontes diferentes:
3.000 barras de 15m cobrem cerca de 31 dias; 1h, 125 dias; 4h, 500 dias.
Os mercados/timeframes se sobrepõem; não são nove amostras independentes.

## Registro e replay

Antes de avaliar resultados, um diário próprio congela o catálogo, hashes dos
dados/páginas e implementação, seis regras, custos, risco, partições e política
estatística. A família inclui **54 tentativas e 324 testes**: seis regras × nove
séries × três cenários × dois benchmarks. As seis tentativas ETH 4h bloqueadas
continuam no denominador; recebem p conservador 1. Não se escolhe a família
somente depois de conhecer as vencedoras.

SQLite de criação exclusiva, transações, triggers e encadeamento de hashes
preservam falhas e interrupções. Não há retry/reset de uma suíte iniciada nem
alteração dos registros. O hash não é atestação externa contra administrador
capaz de reescrever tudo. A família cobre esta suíte, **não** todas as buscas
históricas em outros projetos/campanhas; essa limitação permanece explícita.

As duas janelas de validação de desenvolvimento são as da PR #88, com 25 barras
de separação, acima do horizonte máximo das matrizes. Os primeiros 40% e depois
60% fornecem contexto passado; não há ajuste de parâmetros nessa entrega. Os
últimos 20% ficam reservados e **não são avaliados**. O arquivo inteiro é lido
para validação/hash; não se afirma ocultação física ou que ninguém viu a faixa.

Cada candidata e sua matriz recebem os mesmos custos e atraso em cada cenário:
nominal, comissão/slippage/funding dobrados, e custos dobrados com sinal uma
barra atrasado. A memoização guarda apenas decisões do prefixo fechado já
disponível; testes comparam a saída às regras originais e recusam holdout.
Nenhum global ou módulo S2/S3 é modificado.

## Estatística e interpretação

A série econômica usa mudanças de patrimônio hipotético líquido no fechamento
de cada barra, divididas pelo capital inicial. Inclui perdas/ganhos de posições
abertas e estima slippage de saída, comissão de ida e volta e funding. As marcas
de fim de janela entram nessa série para não esconder perdas abertas; continuam
fora da contagem separada de trades encerrados. O total reconcilia com o replay.
DD no fechamento não mede drawdown intrabar ou risco de liquidação real.

Para cada cenário, testa-se excesso médio contra caixa e contra a matriz sob
custos/atraso iguais. O diagnóstico usa bootstrap estacionário circular com
blocos de comprimento geométrico esperado 8, 16 e 32, 9.999 replicações e seed
fixa. Cada janela é reamostrada separadamente, sem atravessar a separação entre
janelas; as séries são centradas por janela sob a hipótese nula. Sensibilidade
predefinida usa o maior p e o menor limite inferior pontual entre os três blocos.
Média não positiva, série degenerada ou menos de 256 barras por janela recebem
p conservador 1, com o motivo explícito.

Os p-valores são **aproximações condicionais** a estacionariedade e dependência
fraca, não testes exatos ou premissas verificadas de mercado. Os intervalos de
95% são pontuais, não simultâneos. A correção Holm opera sobre os 324 testes
registrados, incluindo falhas, e não exige independência entre testes se seus
p-valores marginais forem válidos. Ela não conserta premissas inválidas do
bootstrap. O algoritmo não é apresentado como implementação do Reality Check
global de White nem como avaliação independente por outro pesquisador.

Uma candidata só passa pela triagem histórica se, além dos critérios descritivos
S5, todos os seis testes ajustados forem <=0,05, DD líquido no fechamento de cada
cenário <=5%, cobertura completa e nenhuma tentativa falha. Isso é um filtro de
pesquisa conservador, não uma autorização operacional. `profitability_proven`,
`execution_authorized`, `automatic_promotion_authorized`,
`statistical_significance_established` e `prospective_evidence` permanecem falsos.

## Uso e reprodução

```bash
cd backend
python -m scripts.gate_s4_capture_history --output /analysis/s4/history --bars 3000
python -m scripts.gate_s4_validate_history register --root /analysis/s4/suite \
  --catalog /analysis/s4/history/catalog.json --output /analysis/s4/manifest.json
python -m scripts.gate_s4_validate_history run --root /analysis/s4/suite \
  --output /analysis/s4/review.json
python -m scripts.gate_s4_validate_history report --root /analysis/s4/suite
```

O comando de relatório verifica a fonte registrada no diário e não reexecuta
trades. Uma suíte já iniciada não pode ser reiniciada. Para reproduzir uma
avaliação, criar outro diretório explicitamente identificado como reprodução,
com os mesmos dados e implementação; não contabilizá-la como evidência nova.
`run --source-root /novo/local/history` permite relocalizar arquivos arquivados,
mantendo os hashes de conteúdo registrados. O diário original é preservado.
O relatório depende do código exato registrado; alterações exigem migração ou
outra suíte, sem reescrever o passado.

O resultado de mercado e o pacote de dados/diário ficam no artefato de evidências
da execução. Testes automatizados usam fixtures sintéticas e não fazem GETs de
mercado. Nenhuma coleta recorrente ou novo serviço é criado.

## Referências primárias

- Holm (1979), *A Simple Sequentially Rejective Multiple Test Procedure*:
  https://www.ime.usp.br/~abe/lista/pdf4R8xPVzCnX.pdf
- Sullivan, Timmermann e White, discussão de bootstrap estacionário e data snooping:
  https://researchonline.lse.ac.uk/id/eprint/119144/1/dp303.pdf
- White (2000), *A Reality Check for Data Snooping* (contexto, não identidade de algoritmo):
  https://doi.org/10.1111/1468-0262.00152
- Contrato público Gate API v4 e origem Testnet, preservados dos adaptadores existentes:
  https://www.gate.com/docs/developers/apiv4/en/futures/
