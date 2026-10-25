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

## Redução de persistência após o segundo pré-deploy

O candidato `aeeb1c7` aprovou os dois esquemas isolados, mas a tentativa nativa
`b8d5c759-6bf1-4cc1-aa10-fa2b074392d8` reprovou com
`PRIMARY_DEADLINE_EXCEEDED`: etapa de 3 s, ciclo total de 8.464,779 ms.
Job/backend em Europa, PostgreSQL recuperado em SFO; 39 amostras de rede
indicaram 140–155 ms. Isso não é latência SQL nem percentil de decisão.
O backend permanece em `044839f`; a validação operacional continua pendente.

As alterações seguintes preservam os mesmos limites e componentes:

- Novos jobs PRIMARY já nascem com lease e INPUT na mesma transação. Em
  PostgreSQL, um CTE INSERT/RETURNING encadeia a inserção do job e de sua memória
  em um comando. ON CONFLICT não altera jobs antigos, donos, tentativas ou
  memórias. Jobs existentes/retries continuam pelo claim explícito com locks.
- Inserções de memórias, tentativas e outputs são agrupadas sem buscar IDs
  gerados que o chamador não utiliza. Validação de propriedade, contagem de
  tentativas, locks, constraints e commit atômico permanecem obrigatórios.
- O processo PRIMARY reservado responde a um handshake no startup, antes de
  o estado operacional aceitar decisões. Usa o mesmo único slot, sem executar
  agentes ou criar réplicas. Startup tem limite separado de 5 s; o lote crítico
  continua limitado a 3 s, incluindo uma reposição necessária após falha.
  Falha/cancelamento do aquecimento encerra o filho e impede boot bem-sucedido.
- Cada etapa PRIMARY exporta tempo, comandos SQLAlchemy, checkouts e chamadas
  de commit. O contexto é isolado dos workers SHADOW. Não são registrados SQL,
  argumentos, URLs ou erros livres; falhas preservam a etapa cancelada. Esses
  contadores não incluem handshake/BEGIN/pre-ping nem equivalem a round trips.
- A fixture positiva emite seu candle sintético com o relógio real após o
  preparo do histórico e o teste negativo. Não modifica o histórico, usa os
  mesmos limites operacionais de frescor e recusa preparo que criaria um gap.
  O erro positivo não inclui mais o CLOCK_UNTRUSTED esperado do teste anterior.

Reprodução local do perfil de persistência, sem conexão hospedada:

```bash
python -m app.validation.primary_persistence --samples 10 --delay-ms 140 --output primary.json
```

Essa fixture usa SQLite temporário, transporte Redis falso e três PRIMARY
determinísticos. O atraso é um await controlado por comando SQL e commit, não
uma emulação exata de PostgreSQL remoto. Não executa risco, OMS ou ordens. Em
cinco amostras exploratórias antes/depois, a etapa final caiu de 13 para 6
comandos SQL, o total de 25 para 14 e as transações de 9 para 8. Com 140 ms
injetados, o tempo caiu aproximadamente de 5,33 para 3,16 s. **O resultado ainda
não certifica o prazo de 3 s.** O CTE específico PostgreSQL não participa dessa
medição SQLite. O benchmark de 300 agentes continua separado e agora informa
explicitamente o tempo de inicialização, fora do temporizador da decisão.

Testes adicionais cobrem reserva idempotente concorrente, dono preexistente,
rollback entre job/INPUT e entre tentativas/memórias/outputs, startup e seu
cancelamento, isolamento dos contadores e diagnóstico sem credenciais. Os
cenários de claims também rodam em PostgreSQL 17 local quando o CI fornece
POSTGRES_TEST_URL; hosts remotos são recusados. Nenhuma migração foi criada.

Antes de novo deploy, exigir CI completo e revisar o perfil. Publicar o job
isolado ou o backend continua sujeito à autorização específica; não mover
regiões/volumes nem ampliar recursos para encobrir o gargalo. Se o orçamento
continuar inviável no perfil nativo, discutir a topologia com evidência em vez
de aumentar silenciosamente timeout ou validade. Fase C continua bloqueada.

## Publicação agrupada após o perfil hospedado de 25/10/2026

O candidato `9083c8d` foi executado no job de auditoria, deployment
`227a630a-4f20-4de1-9285-091f23dbe149`. Os dois esquemas passaram pelas 15
migrações, mas a análise nem chegou a iniciar: `create_jobs` levou 1.060,883 ms
e `requested_events` foi cancelado após outros 2.560,707 ms. O perfil PRIMARY
totalizou 3.622,742 ms incluindo limpeza de cancelamento, com prazo de admissão
inalterado de três segundos. Não foi observada uma ordem PAPER esperada nem
foi certificada a conclusão dos 300 agentes. Banco recuperado em SFO e job em
Europa continuam sendo um risco material; não atribuir a falha a SHADOW sem
evidência. A versão publicada do backend continua `044839f`.

Para um lote PRIMARY recém-reservado, REQUESTED e STARTED agora são publicados
no mesmo lote ordenado antes da análise. Os três eventos REQUESTED mantêm o
estado lógico PENDING/attempt 0, inclusive se um chamador concorrente encontrar
uma lease já reservada. Os três STARTED mantêm LEASED e a tentativa vigente.
Cada evento conserva seu ID determinístico, tópico e registro imutável.
Journal, confirmação do broker e gravação do outbox continuam obrigatórios.
Não se posterga auditoria para depois de uma ordem. A etapa de conclusão ainda
publica seus três eventos, para um total de nove eventos em uma execução normal.
Falha em qualquer confirmação impede análise e a admissão de ordem. A lease e
o INPUT duráveis continuam disponíveis para retomada idempotente. Retomar uma
lease expirada mantém o número de tentativa não concluída conforme a semântica
existente, e não cria outra tentativa nem altera o INPUT.

A API pública `submit_many` continua publicando REQUESTED antes de retornar;
claims de jobs existentes e o caminho legado conservam sua sequência anterior.
A otimização é limitada ao lote novo que a via PRIMARY já possui. Não altera
filas SHADOW, OMS, risco, migrações, esquema, banco/Redis, pool ou prazos.

O perfil agora separa, dentro de cada publicação, espera pelo coordenador,
journal, broker, confirmação, registro de falha e entrega local. Tempos e
contadores SQL são preservados se uma parte for cancelada. Nomes/campos são
fixos; nenhum SQL, payload, credencial ou erro livre é exportado. A duração do
journal/confirmation inclui espera pelo pool, comandos, commit e limpeza; o
contador de checkout não mede isoladamente o tempo de espera por conexão.
As partes são subdivisões da etapa; não somá-las novamente ao tempo da etapa.
Essas medidas só representam Railway quando o novo código for executado lá.

Uma comparação local sequencial, dez amostras antes e dez depois, três PRIMARY
determinísticos, SQLite temporário, transporte falso e 140 ms por comando SQL
e chamada de commit, encontrou:

| Persistência do lote | Antes (`9083c8d`) | Publicação agrupada |
|---|---:|---:|
| P50 | 3.149,172 ms | 2.581,086 ms |
| P95 / P99 exploratórios | 3.360,979 ms | 2.620,534 ms |
| Comandos SQLAlchemy | 14 | 12 |
| Checkouts / chamadas de commit | 8 / 8 | 6 / 6 |
| Eventos de ciclo de vida | 9 | 9 |

Essa medição isola a persistência dos agentes: não inclui risco, OMS ou ordens,
não usa PostgreSQL remoto nem Redis real e não certifica o prazo hospedado.
As amostras não foram intercaladas e os percentis de cauda são exploratórios.
O perfil base usou apenas instrumentação passiva em relação a `9083c8d`.
Uma nova execução hospedada requer CI completo e autorização específica para o
job; usar novos nomes de cópias para a revisão nova, preservando as anteriores.
Publicar o backend continua exigindo validação satisfatória e aprovação própria.

## Diagnóstico nativo e comparação regional preparada

O deployment `ce38544a-6e14-4828-b895-d3c57a99d6dc`, candidato `c6f5b77`,
rodou na Europa em 25/10/2026. Os dois esquemas isolados passaram, mas o fluxo
reprovou antes da análise com `PRIMARY_DEADLINE_EXCEEDED`. O perfil contém:

| Etapa | Tempo | Resultado |
|---|---:|---|
| create_jobs | 1.209,313 ms | COMPLETED |
| admission_events / lock_wait | 0,063 ms | COMPLETED |
| admission_events / journal | 1.006,444 ms | COMPLETED |
| admission_events / broker | 1,344 ms | COMPLETED |
| admission_events / confirmation | 1.368,136 ms | CANCELLED |
| PRIMARY total, incluindo limpeza | 3.586,830 ms | CANCELLED |

O ciclo positivo totalizou 8.501,475 ms, com 4.906,994 ms antes dos agentes.
Redis e espera pelo coordenador não foram o gargalo observado. O tempo de banco
inclui aquisição de conexão, protocolo, SQL, transação e limpeza; o perfil não
isola qual parcela interna domina nem demonstra sozinho um defeito do servidor.
Job/backend estão em Europa e o PostgreSQL recuperado em SFO. A causalidade da
topologia deve ser testada; não declarar que o ganho local resolveu o Railway.
Nenhuma ordem esperada foi admitida e não há percentis de ordens hospedadas.
As cópias da revisão continuam preservadas e não devem ser reutilizadas/limpas.

Para tornar um experimento regional verificável, o gate exporta o commit e a
região da réplica nos eventos de esquema, sucesso e falha, com campos validados
e sem valores livres de ambiente. A opção `PAPER_VALIDATION_EXPECTED_REGION`,
quando configurada, exige igualdade exata com `RAILWAY_REPLICA_REGION` antes
de abrir uma conexão ou criar cópia. Região ausente, inválida ou divergente
resulta `PAPER_VALIDATION_REGION_MISMATCH`. Quando a opção não existe, o caminho
anterior continua disponível. Essa mudança afeta apenas o validador, seus testes
e esta documentação; o código operacional em `backend/app`, exceto testes,
é idêntico ao candidato c6f5b77.

Proposta para aprovação, sem aplicação implícita: executar somente o job de
auditoria existente, sem volume/endpoint, na região `sfo` onde o banco já está,
uma réplica, restart NEVER, mesmo timeout de 1.200 s, pool e credenciais. Usar a
nova revisão fixada e cópias com seus novos nomes, sem apagar as da Europa, e
`PAPER_VALIDATION_EXPECTED_REGION=sfo`. O guard evita certificar uma execução
em região diferente da aprovada. Não mover PostgreSQL, Redis, backend ou volume,
nem contratar plano/recurso ou aumentar réplicas totais. O teste consome a
capacidade faturável já existente e pode reprovar por outro limite.

Comparar os subtempos e resultado completo com c6f5b77 na Europa. A fixture e
o código operacional permanecem os mesmos, mas são execuções sequenciais em
instâncias/regiões distintas, não um experimento controlado com desempenho
garantido. Redis poderá se tornar remoto ao job. Um teste aprovado em SFO
**não certifica o backend que permanece na Europa**: apenas fornece evidência
para decidir a topologia final, que exigirá proposta, validação e autorização
próprias. A Fase B operacional e a Fase C continuam bloqueadas.
