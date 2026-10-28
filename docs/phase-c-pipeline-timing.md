# Fase C — fluxo simultâneo, validade e tempo até o fill PAPER

Esta implementação sucede a revisão de prontidão. Mantém estratégias, pesos,
limites, saldo, infraestrutura e fronteira PAPER. Não declara toda a Fase C
certificada nem rentabilidade; não publica automaticamente um novo runtime.

## Problema e comportamento resultante

A checagem de idade antes de chamar o OMS podia ser seguida por espera na
consulta de ordem, auditoria ou lock do portfólio. Uma aprovação com TTL de
60 segundos ainda poderia estar válida após o sinal de mercado vencer.

Quando Office está ativo em PAPER, o prazo do sinal agora acompanha a chamada
de risco: fechamento do candle + limite de atraso já configurado. O prazo entra
no fingerprint e na evidência imutável do risco. A aprovação usa o menor valor
entre esse prazo e o TTL existente. Uma repetição com prazo diferente viola a
idempotência; não renova uma aprovação. Nenhuma migração é necessária.

O consumo já revalida a validade dentro da transação, após adquirir os locks de
controle/portfólio e da aprovação. Com o prazo reduzido, esse mesmo mecanismo
bloqueia sinais que venceram durante a espera. Expiração de sinal é erro de
qualidade de dados, sem afirmar que o banco ficou indisponível. A rejeição não
consome a aprovação nem insere uma ordem.

Esse é um corte de admissão sob lock. Não é promessa de acknowledgement de
commit ou fill em exchange antes de um horário absoluto; o retorno da transação
pode ocorrer depois da checagem. A medição abaixo registra o retorno real.

## Instrumentação

O status do orquestrador expõe `paper_execution_timing`, somente com medições
de ciclos Office/PAPER. Mantém as últimas 100 tentativas, com uma linha por ciclo:

| Marco | Fronteira observada |
|---|---|
| received | Entrada em on_candle_closed, antes da espera do lock |
| queue_acquired | Aquisição do lock compartilhado do ciclo |
| decision | Candidato final consolidado, antes de sua persistência |
| risk_started / risk_completed | Chamada obrigatória de risco |
| submit_started | Entrada no OMS, incluindo sua consulta de idempotência |
| paper_fill_committed | Retorno da transação durável e registro da posição PAPER |
| terminal | Término, rejeição, falha ou cancelamento do ciclo |

Offsets usam o mesmo relógio monotônico do processo. Não são calculados a partir
do horário do candle, opened_at, created_at ou timestamp de exchange. A origem
da medição é o recebimento no orquestrador, não o socket de mercado.

Somente fills identificados e com todos os marcos ordenados entram nos percentis
de recebimento até fill. Bloqueios, erros, cancelamentos e duplicidades continuam
como tentativas, sem criar observações de latência zero. Sem amostra, P50/P95/P99
ficam nulos. Há contagem e percentis por 15m, 1h e 4h.

As linhas contêm identidade de correlação, decisão, estratégia, ativo, timeframe
e ordem, com códigos de erro fixos. Não incluem preços, saldos, SQL, credenciais
ou texto de exceção. ContextVar e fechamento da linha impedem mistura entre
correlações e alteração tardia por tarefas filhas. Uma falha de publicação após
o commit não transforma o fill existente em ausência de ordem.

O buffer é volátil: reinicialização zera as medições, preservando as ordens e a
exposição duráveis. Capturas externas podem reter o buffer. Não é um diário
durável completo de latência, um SLO de produção ou medição de venue.

Uma submissão sem marco de commit é `UNCONFIRMED`: um cancelamento ou falha
durante o retorno do banco não prova ausência de ordem. A reconciliação do livro
durável continua sendo a autoridade. Somente o retorno observado com identidade
de ordem é `COMMITTED`; tentativas anteriores ao envio são `NOT_SUBMITTED`.

## Verificações desta entrega

- Três mesas reais, 300 agentes registrados, PRIMARY, decisão, risco, OMS,
  ordens duráveis, HTTP de leitura e cadeia de auditoria no mesmo contexto.
  Fixtures usam BTC/15m, ETH/1h e SOL/4h com limites menores só no teste, permitindo
  três entradas sem ultrapassar o livro compartilhado. Replay não duplica fills;
  reinicialização recupera as três posições e mantém o saldo único.
- Mesma verificação em PostgreSQL 17 e Redis de CI, em banco novo de template0;
  hostnames remotos são recusados. A prova anterior de última capacidade com
  três conexões independentes e direções iguais/opostas continua na suíte.
- Sinal vence na fila: nenhuma ordem e nenhuma amostra de fill.
- Sinal vence dentro da consulta do OMS: prazo durável reduzido impede a ordem.
- PostgreSQL: uma sessão segura o lock real da carteira; outra é observada em
  espera via pg_stat_activity. O relógio do gate avança somente depois dessa
  comprovação. Após liberar o lock, a admissão é rejeitada, sem consumo ou ordem.
  O avanço controlado evita um sleep de 30 segundos e não representa latência
  de produção medida.
- Cancelamento enquanto espera e falha de publicação após commit verificam os
  dois resultados diferentes, inclusive a preservação do fill confirmado.
- TTL nunca é ampliado; prazo sem timezone, vencido ou alterado na mesma
  identidade é rejeitado. Métricas têm janela limitada e dados destacados.

## Conflitos, aprovações pendentes e recuperação

O contrato preserva a política existente. Cada ordem pertence à sua estratégia,
ativo e timeframe; não existe prioridade financeira por mesa. Compra e venda
opostas não fecham ou revertem automaticamente uma à outra. Continuam somadas
na exposição bruta e por ativo, mesmo quando a exposição líquida diminui. O
monitor de SL/TP continua independente por ordem, a partir dos candles do ativo.

Uma aprovação pendente é autorização limitada por prazo e snapshot, **não uma
reserva de saldo**. Ela não garante capacidade futura. No consumo, o lock global
durável serializa a admissão e revalida o livro. Entre aprovações independentes
sobre o mesmo snapshot, a primeira admissão durável ganha; as demais ficam
obsoletas. Não há promessa de FIFO entre processos nem retry automático com
nova identidade. Redução e bloqueio pertencem ao RiskManager existente. O teste
PG usa três pools e aprovações simultâneas, tanto iguais quanto opostas: uma
aceita, duas obsoletas, exposição única recuperada de 4.000 sobre 10.000. Os
limites dessa fixture não são a configuração operacional.

O probe de recuperação termina de verdade um processo Python com `os._exit`
enquanto outras duas mesas aguardam o lock do ciclo. Quatro fronteiras:

| Queda | Estado obrigatório após reconstrução |
|---|---|
| Antes do consumo | Nenhuma ordem, aprovação ACTIVE |
| Após flush SQL, antes do commit | Rollback de ordem/espelho/consumo; aprovação ACTIVE |
| Após commit, antes do acknowledgement à engine | Uma ordem, um espelho OMS, aprovação CONSUMED |
| Após registrar o livro local, antes do retorno OMS | Mesma ordem única, exposição restaurada |

Nos casos confirmados pelo banco, a consulta/repetição OMS recupera a mesma
identidade sem reenviar. Replay do primeiro candle persistido não cria ordem.
Um candle cuja execução foi interrompida não é automaticamente reexecutado:
isso pode perder um sinal, e deve continuar visível como limitação de cobertura.
Não existe reserva pendente para liberar. Leases dos agentes permitem repetir
análise, não concedem autoridade para repetir execução. A suíte também verifica
as três chegadas concorrentes com exceções explícitas de banco, broker e auditoria:
zero ordens antes e depois da reconstrução. Exceções não simulam falha de rede;
queda do processo não representa queda do servidor PostgreSQL ou do host.

## Validação hospedada delimitada

`scripts/validate_railway_phase_c.py` testa o candidato usando o job de auditoria
existente e 11 bancos PG17 novos de template0, prefixo exclusivo por revisão.
Recusa os nomes anteriores, região diferente, ausência de opt-in e migração no
principal. Verifica o schema principal por SELECT, mantém os bancos de evidência
e limita seu total a 200 MB. Recusa execução se os bancos do cluster mais esse
orçamento ultrapassarem 3,8 GB. Não cria serviços, volumes nem recursos pagos.
Configuração local de fixture (20% por posição/estratégia) fica só nesses bancos;
PAPER e prazos 3s/5s/120s permanecem obrigatórios. HTTP externo fica bloqueado.

O job reúne pipeline simultâneo, conflitos em três pools independentes, expiração
após espera real por row lock, falhas concorrentes e as quatro mortes de processo.
Seu PASSED atesta somente o candidato isolado. Não publica o backend nem encerra
a avaliação financeira, causal ou fora da amostra.

## Publicação e critérios restantes

Sem migração ou nova variável. Antes de publicar, revisar o commit exato e seu
CI, o menor prazo das aprovações novas e o novo campo de status. Ordens abertas,
backups e histórico existentes permanecem preservados. A campanha externa
atual está fixada no SHA certificado anterior: nova publicação requer uma nova
janela identificada de observação, sem sobrescrever sua baseline.

Antes do encerramento técnico, o candidato final precisa passar nesse job,
ser publicado em PAPER e ter revisão/readiness e preservação do livro verificadas.
Evidência estatística fora da amostra e qualidade financeira continuam dependentes
de dados: não podem ser declaradas aprovadas por fixtures ou tempo decorrido.
