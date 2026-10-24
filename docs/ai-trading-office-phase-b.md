# AI Trading Office — Fase B

Esta alteração parte integralmente da Fase A (`d8b4917`, PR #39), na branch
`feat/ai-trading-office-phase-b-20261024`. O staging continua em `044839f` até
aprovação explícita. Não há migração, novo serviço, volume ou plano contratado.

## Duas vias e limites de autoridade

`OFFICE_ASYNC_ENABLED` é **false por padrão**. O caminho anterior continua
disponível para comparação e rollback. Com a opção habilitada, somente em PAPER:

1. O candle passa pelas verificações existentes de relógio, qualidade, persistência,
   duplicidade, estado operacional e posições. Eventos concorrentes são serializados
   sobre o portfólio compartilhado.
2. A via crítica executa somente MarketDataAgent, QuantAgent e TrendAgent, com
   submissão/execução duráveis e capacidade reservada. Não espera os 297 SHADOW,
   seus forecasts, scorecards, pesquisa ou persistência de avaliação.
3. Estratégia → candidato → auditoria → **RiskManager obrigatório** → **OMS** →
   simulador PAPER mantêm os controles existentes. A validade do candle é revalidada
   antes do risco e novamente antes do envio ao OMS. Mercado desconectado bloqueia.
4. Após a decisão, a admissão analítica não bloqueante recebe o candle e outputs
   PRIMARY. O lote SHADOW é processado em segundo plano pela mesma fila durável,
   sem autoridade para ordens ou alterações de estratégia.

O consenso operacional CONFIRMATION não é silenciosamente ignorado. Enquanto não
houver integração aprovada de confirmação assíncrona, esse modo resulta WAIT com
`OFFICE_CONFIRMATION_NOT_READY`. O modo SHADOW atual permanece observacional.
Promoção de modelos, regras das mesas e nova construção de portfólio não pertencem
a esta fase.

## Filas, recursos e recuperação

- Na fila durável existente, PRIMARY tem prioridade sobre SHADOW. A via crítica
  também pode reclamar seus próprios jobs, sem esperar um slot do worker SHADOW.
- Capacidade efetiva analítica: até dois slots, um job por claim. Isso evita alugar
  antecipadamente 32 jobs que poderiam expirar enquanto aguardam análise.
- Buffer de admissão: 12 lotes, com prioridade 15m/1h/4h, deduplicação por candle e
  rejeição explícita por saturação. É um buffer de observação, não confirmação de
  entrega durável. `accepted` significa aceito nesse buffer; jobs confirmados estão
  na fila existente do Repository.
- Um lote já em andamento não é preemptado por outro timeframe. O prazo limita o
  trabalho pendente; PRIMARY conserva prioridade e capacidade independentes.
- Prazo analítico padrão: 120 s desde o candle. Jobs vencidos terminam SKIPPED com
  evidência; seus históricos não são apagados. Deadlines inválidos são rejeitados.
- No restart, os candles duráveis recentes dos streams aprovados reconstroem a
  admissão. Correlation/idempotency determinísticas evitam duplicar jobs já
  admitidos. Somente análise é recuperada: candles históricos nunca são reenviados
  à execução de trading. Jobs/leases já persistidos retomam pelo worker existente.
- Snapshots de candles excluem observações posteriores ao `as_of` do request.
  Evidências externas continuam pelos mesmos gates de proveniência e validade.
- Os resultados elegíveis podem gerar `OfficeAnalysis` da Fase A, com referência
  ao job durável, validade e revisão explícita. Sem revisão de código configurada,
  o cache não inventa uma versão. O cache de contexto é limitado, observacional e
  reconstruível; não é necessário para aprovar uma ordem nesta fase.

O tráfego adicional não recebe novo pool SQL. A configuração PostgreSQL precisa
de pelo menos quatro conexões, com até dois workers analíticos. O staging recuperado
já usa quatro; nenhum parâmetro foi alterado remotamente. SQLite em arquivo usa
uma sessão por vez para respeitar sua restrição de escritor único e evitar
`database is locked` nos testes concorrentes; PostgreSQL permanece com seu pool.

## Agentes lentos e travados

Análise isolada em processos Python supervisionados:

- Um processo reservado aos PRIMARY e até dois aos SHADOW; criados sob demanda e
  reutilizados. Nenhum agente SHADOW pode ocupar o processo reservado.
- Apenas snapshots OHLCV, evidência governada e estado de conexão atravessam a
  fronteira. Repository, credenciais, OMS e RiskManager não são transportados.
- Timeout PRIMARY total: 3 s por lote crítico; timeout analítico: 5 s por execução,
  incluindo snapshot/prefetch/IPC. O supervisor cancela e encerra o processo travado,
  inclusive um `while True` sem `await`; uma coroutine cancelável não é tratada
  como prova suficiente de isolamento de CPU.
- Três falhas abrem breaker individual por 60 s. Falha/timeout PRIMARY bloqueia
  operações; SHADOW indisponível não impede a decisão crítica.
- Leases precisam superar o timeout e uma margem de confirmação. Configurações
  incompatíveis falham no boot, em vez de criar corridas entre workers.
- Shutdown cancela a admissão, preserva jobs duráveis, encerra workers e processos
  e fecha conexões. A limpeza de lifespan ocorre também quando o corpo lança erro.

Isso é isolamento de falha para código do projeto, não um sandbox para executar
código Python hostil. Há custo de memória/CPU no serviço existente: até três
processos adicionais, sem novos serviços. Esse impacto deve ser considerado na
aprovação de publicação; não se promete menor fatura.

## Atualização de mercado compatível

Com a opção ligada, a assinatura pública WebSocket deriva de estratégias
habilitadas e símbolos permitidos: SCALP_15M, DAY_1H e SWING_4H produzem nove streams
para BTC/ETH/SOL em 15m/1h/4h. A assinatura isolada em 5m deixa de ser usada nessa
configuração; não se inventa estratégia para 5m.

Dados públicos chegam pelo WebSocket existente conforme enviados pela fonte,
com filas e persistência raw já existentes. A decisão usa candle **fechado** no
timeframe da estratégia; não há promessa de operar a cada segundo nem de alterar
as metodologias. O status expõe as assinaturas calculadas. Histórico/hidratação,
gaps e relógio continuam obrigatórios. Não há integração de novas mesas na Fase C.

## Validação e medição reproduzível

```bash
python -m pytest app/tests -q
python -m app.validation.office_latency --samples 40 --output latency.json
```

O benchmark compara a via síncrona preservada e a via rápida no mesmo host, com
dados OHLCV determinísticos, os 300 agentes reais e um SHADOW de 100 ms controlado.
Cada amostra cria uma ordem PAPER real pelo risco/OMS do sistema. As duas variantes
completam os 300 jobs; a conclusão analítica posterior é registrada separadamente.
Há aquecimento, alternância da ordem de execução e métricas brutas por amostra.

P50/P95/P99 são latências do caminho crítico até a ordem, não da fórmula isolada.
O cold start é registrado separadamente. O benchmark usa repositório em memória e
MockTransport HTTP, sem consultas a corretoras. Não representa a rede Railway EU
↔ PostgreSQL SFO. A média anterior de cerca de 92 s no staging não pode ser
subtraída desses percentis locais para afirmar um ganho em produção.

Amostras pequenas têm P99 exploratório, não constituem SLO. As integrações nativas
do CI usam PostgreSQL 17 e Redis descartáveis e validam BUY, SELL, auditoria,
restart e os 300 jobs nas configurações anterior e assíncrona. Os resultados
financeiros sintéticos dessas fixtures não demonstram rentabilidade.

Os casos novos incluem loop CPU infinito comprovadamente iniciado, morte e
reposição de processo, breaker, operação PAPER durante análise travada,
deduplicação concorrente, portfólio único, timeout crítico, sinal vencido depois
do risco, falhas de banco/broker/auditoria/risco/mercado, deadlines inválidos,
snapshots sem look-ahead, replay analítico e assinaturas compatíveis.

## Publicação e escopo

Publicar exige aprovação explícita do commit com CI aprovado e do impacto:
`OFFICE_ASYNC_ENABLED=true`, nove streams e até três processos supervisionados no
serviço existente. Não trocar banco/Redis, ampliar plano ou mover volumes.
Rollback: desabilitar a opção ou voltar ao commit de referência, preservando jobs,
decisões, ordens e todos os volumes. Sem restauração destrutiva de dados.

A Fase C não é iniciada nesta entrega. Mudanças de estratégia, novos módulos de
análise técnica, promoção, dashboard e comparação financeira A/B/C/D continuam
fora do escopo. O resultado desta fase é validado em branch/CI; a latência real
após deploy só pode ser confirmada depois da publicação autorizada.

## Correção após o pré-deploy hospedado de 24/10/2026

O primeiro job isolado em Railway, commit `bf10707`, aprovou os dois esquemas,
mas falhou antes de produzir uma ordem. A inspeção somente leitura da cópia
retida encontrou dois PRIMARY concluídos cerca de 5,25 s após a criação dos jobs
e o terceiro ainda PENDING, sem decisão ou ordem nova. O código permitia que os
dois workers analíticos reservassem PRIMARY e que a via inline esperasse pelas
suas leases; a ordenação por prioridade não reservava a capacidade.

Com Office ativo, o filtro SHADOW agora é aplicado no claim durável do worker,
tanto em memória quanto em SQL. A via PRIMARY reserva somente os IDs do seu
próprio lote e reutiliza a execução e conclusão atômica em lote, mantendo leases,
tentativas, memórias, eventos determinísticos e controle de idempotência. Não
relê o trace completo de cada job novo. Um consumidor explícito pode retomar os
mesmos IDs após expirar uma lease; isso registra o resultado do agente, sem
reexecutar uma ordem. Os workers analíticos não retomam PRIMARY órfãos sozinhos.
As APIs genéricas de lote continuam aceitando coortes mistas e preservam a ordem.
O comportamento do modo legado, sem Office, permanece sem filtro de papel.

Os prazos e as checagens de frescor, risco e OMS não foram aumentados ou
contornados. A latência de rede Europa–banco observada, aproximadamente
136–151 ms, continua um risco a medir na nova validação. A correção da disputa
não é uma certificação de que toda a cadeia durável cabe nos prazos hospedados.

O gate agora exporta coordenadas de código e códigos permitidos quando falha,
sem mensagem de exceção, SQL ou URL. O fluxo nativo verifica que as três
tentativas PRIMARY foram executadas pelo worker reservado, além dos invariantes
PAPER anteriores. Essa nova versão ainda exige CI e pré-deploy aprovado antes
de publicar o backend; as cópias anteriores permanecem preservadas.

O CI também expôs uma leitura inconsistente no caminho legado: em READ
COMMITTED, um worker podia concluir entre a leitura do job e a leitura das
tentativas/memórias. A leitura de traces PostgreSQL agora usa uma transação
somente leitura em REPEATABLE READ, com isolamento limitado à conexão dessa
operação e restaurado ao devolver a conexão ao pool. Um teste nativo força o
commit exatamente entre os SELECTs e verifica o snapshot anterior coerente e
o resultado completo numa leitura posterior. Nenhuma invariante é relaxada.
