# Medição financeira V2 — protocolo fixo

Antes do primeiro replay financeiro V2, congelar este contrato e o código no Git.
Regras técnicas: versão do PR #102, sem alteração do observador. Esta é uma
hipótese posterior à V1, avaliada na mesma janela histórica para desenvolvimento;
não é confirmação independente, nem uso da reserva existente.

- Mercado BTC_USDT Gate. Fonte de 01/11/2023 a 15/12/2023; avaliação de 15/11 a
  15/12/2023, fim exclusivo. Dois blocos de 15 dias, sem emenda dos capitais.
- 1m, funding liquidado e mark 4h reutilizados do pacote anterior preservado.
  Seis GETs oficiais declarados antes da coleta recuperam 5m/1h/4h de novembro
  e dezembro. 15m agrega somente grupos completos de três candles de 5m.
- Não preencher minutos ausentes. Falta de 1m cancela cenário/entrada pendente;
  brackets de posição aberta usam o referencial OHLC independente de 5m.
- Três fricções por bloco: taker 5 bp/lado em todas; slippage 6 bp/lado nominal,
  12 bp/lado stress; stress com atraso acrescenta uma barra de 5m. São seis
  referências correlacionadas de dois períodos, não seis amostras independentes.
- Capital 1.000 USDT/caso. Risco planejado 0,25% incluindo custo no stop,
  exposição <=capital, lote hipotético 0,0001 BTC. Mesmos bloqueios de pesquisa
  por perda diária de 2% e drawdown de 10%; critério de avaliação DD <=5%.
- Entrada na primeira abertura de 5m estritamente depois da disponibilidade da
  reconfirmação (+2s após fechamento assumidos), mais atraso registrado. Usar
  somente observações recebidas antes da abertura. Revalidar gatilho, contexto,
  prazo e ausência de toque em stop/alvo; recusar plano expirado na abertura.
- Stop/alvo congelados da V2. Em ambiguidade, stop primeiro; gap adverso usa
  abertura pior e alvo em gap favorável permanece limitado ao alvo. Holding
  máximo seis horas; cooldown uma hora após saída; sem posições sobrepostas.
- Funding assinado usa taxa liquidada e mark de abertura de 4h. Extremos dos
  candles adjacentes produzem limites condicionais, não ticks exatos ou ICs.
  Evento na abertura de entrada/saída tem ponto zero e limites incluindo zero
  e pagamento integral. Mesma contabilidade do referencial V1.
- Exigir >=30 trades fechados por caso, líquidos fechado/marcado inferiores
  positivos, fator de lucro >=1,2, maior vitória <=50% dos ganhos e DD <=5%.
  Sem trades, taxa de acerto e fator de lucro são indefinidos. Estimativa de
  posição aberta no fim fica fora da contagem de trades encerrados.
- Uma execução de seis casos, orçamento de busca de parâmetros zero. Não
  retentar, resetar, mudar datas/alvos/filtros após resultado ou forçar entradas.
  Nenhuma significância/promoção/S5, LIVE, TESTNET ou alteração em produção.

Preservação obrigatória: antes de STARTED, salvar pacote com dados, recibos,
intenções, este protocolo, registro e SQLite ainda em REGISTERED. O driver
exige identificador do salvamento, hash do ZIP, identidade do registro/SQLite
e hashes de cada fonte dentro do ZIP. Esse recibo é uma referência ao salvamento
observado, não atestação independente do conteúdo pelo serviço. O resultado
final também deve ser preservado imediatamente após os seis casos.

Os testes de engenharia usam dados inventados e não entram no resultado.
Fees pessoais, lotes históricos, books, fills, margem e liquidação não são
reconstruídos. Esta medição estima um referencial; custos de desenvolvimento,
OpenAI e infraestrutura ficam fora do PnL de negociação. Zero trades não será
declarado lucratividade nem resistência comprovada às fricções.
