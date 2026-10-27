# Fase B — publicação e verificação final preparadas

O candidato operacional `6abaf7c` passou no job Railway
`18f35663-d87f-4d33-8454-b0590bba8cc8`, em 27/10/2026 às 09:02:55 UTC.
BUY e SELL em 15m, 1h e 4h concluíram 300 jobs cada, com três PRIMARY reservados,
risco, OMS PAPER, deduplicação, auditoria e reconstrução do estado durável.
As seis decisões levaram 1.443,568 a 2.022,001 ms. São seis fixtures sintéticas,
não uma amostra para declarar SLO, P95/P99 de produção ou rentabilidade.

O backend ainda publicado é `044839f`, na Europa; o PostgreSQL 17 recuperado está
em SFO. O resultado aprovado ao lado do banco não certifica esse backend remoto.

## Mudança operacional para revisão

Recomenda-se migrar o serviço PostgreSQL 17 recuperado e seu volume existente
`b6668722-1232-4eb6-ac67-56c48d3e9378` de SFO para `europe-west4-drams3a`, onde
backend e RedisEU já estão. A documentação Railway informa que o volume acompanha
a região do serviço e que a migração causa indisponibilidade. A janela precisa
de aprovação concreta antes de execução. Não alterar o PostgreSQL 18 legado.

O boot atual do PG17 foi decodificado para inspeção privada: exige PG_VERSION=17
e CLUSTER_OWNER corretos ao encontrar o diretório existente; initdb só existe
no ramo de criação de diretório novo. Não há restore, DROP ou remoção de dados
nesse caminho. Credenciais e o comando codificado não são publicados aqui.

Em 27/10 às 09:13:47 UTC havia 454.877.184 bytes livres no filesystem de
4.838.498.304 bytes (91% ocupado). O principal tinha 1.677.432.499 bytes,
15 migrações, ingest_lag_ms INTEGER, quatro conexões de runtime e nenhum bloqueio.
Não executar nova certificação sobre essa capacidade apertada. A proposta inclui
ampliar o mesmo volume de 5 GB para 10 GB, preservando backups e bancos de evidência.
O painel confirmou Hobby com teto de 5 GB. Portanto essa ampliação requer aprovação
específica de upgrade para Pro, mínimo de US$ 20/mês com US$ 20 de uso incluído;
consumo além disso é cobrado à parte. A documentação atual informa armazenamento
a US$ 0,15/GB/mês sobre uso efetivo. Nenhum upgrade foi autorizado ou executado
nesta retomada. Não presumir que o aumento cabe no plano atual.

Referências oficiais: https://docs.railway.com/pricing/plans e
https://docs.railway.com/volumes/reference. O valor final depende do consumo,
impostos e condições de cobrança mostrados na contratação.

O pacote de recuperação integral externo já foi retido e conferido:
285.296.640 bytes, SHA-256
`a31303804333943050b12fff627e4d9a5c0026c1b4d043781c07e2dcb2c2ab87`.
Não limpar bancos antigos, evidências, arquivos ou volumes para liberar espaço.

Sequência prevista:

1. Aprovar capacidade e janela; suspender novas decisões pelo controle operacional
   existente, conferindo ordens/estado antes da interrupção.
2. Ampliar o volume existente; mover somente o serviço PG17 e seu volume para EU
   Metal; conferir volume, CLUSTER_OWNER, PG_VERSION, contagens e conexão privada.
3. Publicar revisão exata com CI completo no backend existente, com
   OFFICE_ASYNC_ENABLED=true, pool 4 sem overflow, workers 2/batch 1, PAPER,
   TESTNET=false e deadlines PRIMARY 3 s, SHADOW 5 s, TTL 120 s. O pre-deploy
   existente aplica a migração BIGINT com lock_timeout 5 s/statement_timeout 60 s.
   Não iniciar publicação enquanto a movimentação do banco não estiver estável.
4. Conferir a revisão e nove assinaturas de mercado, /ready, integridade e
   preservação de estado após substituição do container; executar o job final
   abaixo na mesma região e com limites de recurso identificados.

Rollback conserva BIGINT, jobs, decisões, ordens e volumes: desabilitar Office
ou voltar ao código anterior. Não restaurar o backup por cima do banco atual,
reduzir BIGINT ou apagar histórico como forma de rollback.

## Job final com armazenamento limitado

Novo entrypoint: `python scripts/certify_railway_office_runtime.py`.
Exige `PHASE_B_FINAL_CERTIFICATION_ENABLED=true`, revisão SHA completa,
`PAPER_VALIDATION_EXPECTED_REGION=europe-west4-drams3a`, região real igual e
`PAPER_VALIDATION_ALLOW_INGEST_LAG_MIGRATION=false`. Todas as 16 migrações do
principal devem estar registradas com hash correto. O principal é consultado
em transação somente leitura; nenhuma migração principal é aplicada pelo job.

São sete bancos novos por revisão, prefixo `capital_cipher_final_b_<sha12>_`:
flow, cpu15m, cpu1h, cpu4h, faultdb, faultbroker e faultaudit. Todos começam em
template0, sem copiar o donor de 247 MB. Antes da primeira criação, todos os nomes
devem estar ausentes. Bancos e evidência ficam retidos; não são apagados ou
reutilizados. O limite agregado de 160.000.000 bytes é conferido entre etapas;
não é um limite rígido do filesystem e não substitui a conferência de espaço livre.

O job repete os seis fluxos reais de 300 agentes e os controles PAPER existentes.
Em três bancos distintos, ocupa o supervisor SHADOW real com loop infinito de CPU,
comprova seu início, executa uma ordem pelo risco/OMS enquanto ele ainda está
rodando, espera o timeout de 5 s, verifica a morte do filho e reconstrói o estado
da ordem fechada. A injeção usa diretamente o supervisor SHADOW do runtime;
não é uma execução durável adicional de agente nem um processo com autoridade
para ordens. Os 300 agentes registrados e suas afiliações permanecem intactos;
a admissão analítica normal continua presente. As seis fixtures
anteriores verificam separadamente a conclusão dos 300 jobs duráveis.

Falhas de banco, broker e auditoria são exceções explícitas na fronteira de
dependência, com comprovação de que ela foi alcançada e nenhuma ordem nova.
Isso não equivale a cortar a rede física ou reiniciar o servidor Redis/PostgreSQL.
HTTP externo é bloqueado durante as fixtures; não se acessa uma venue de execução.
Reconstrução de aplicação não é apresentada como reinício de container: a troca
real do container do backend é verificada separadamente.

PAPER permanece obrigatório, LIVE/TESTNET e a Fase C não são habilitados.
Código, CI e este documento preparam a verificação; sua existência não declara
que a nova publicação ou o job final já foram executados.

O benchmark local mantém o agente de atraso controlado fora da publicação de
OfficeAnalysis, mesmo quando o shell informa APP_SOURCE_REVISION. Esse substituto
é uma fixture, sem afiliação revisada no catálogo de produção. O relatório conserva
a revisão externa como proveniência e declara que não mede publicação do cache.
Isso não altera o catálogo, scheduler, risco ou OMS da aplicação operacional.
