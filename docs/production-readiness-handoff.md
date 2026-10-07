# Retomada da preparação de produção — 2026-10-07

## Pedido e estado

O usuário pediu auditoria, correções, preparação de produção e continuidade se os créditos acabarem. Escolheu **Bybit Futuros, somente TESTNET**, com **Vercel e Supabase**. Não operar dinheiro real nem comprar recursos. **Implantação e operação TESTNET ainda NÃO concluídas.**

Auditoria original: main `4af5904842cebc623ecd1c66e7a0cefeb3557e09`. Esta branch `fix/production-readiness-audit` parte de `c8f88c4ef99f3940789da768b8d3c32631b14953`, branch `agent/fly-paper-staging`, PR #23. Preservar esse trabalho. Estado de saúde histórico daquele PR não comprova saúde atual.

## Correções implementadas

- Stops PAPER em gaps usam a abertura adversa, evitando preços que a vela não negociou. Custos do simulador simples continuam limitados.
- Reinício PAPER restaura ordens abertas, histórico, saldo, drawdown, limites realizados do dia e idempotência antes da ingestão. Dados incompletos interrompem boot. O teste SQLite reabre o banco, executa stop da posição recuperada e verifica outro reinício.
- Ledger detalhado de margem/funding não é persistido: recuperação com histórico nesse modo é rejeitada. Não alegar recuperação completa desse ledger.
- Reconciliação Bybit compara posições mesmo com lista remota vazia. Consultas de ordens linear incluem `settleCoin=USDT`, preservando descoberta de órfãs da conta USDT.
- Estratégias rejeitam regimes fora das listas permitida/reduzida; UNKNOWN e LOW_VOLATILITY não recebem autorização implícita.
- Painel limpa dados de consultas com falha, limita espera, evita sobreposição, informa ausência de telemetria e valida confirmação do kill switch.
- OMS verifica release antes de enfileirar e antes de enviar entradas TESTNET: decisão persistida mais recente, aprovada, mesma revisão e validade temporal. Ausência, expiração ou falha bloqueiam entradas; cancelamentos permanecem possíveis. Perda da autorização no despacho aciona kill switch e quarentena.
- `APP_SOURCE_REVISION` identifica o SHA implantado; vazio bloqueia entradas TESTNET. Imagem Fly aceita o build arg. Nunca inventar atestação/canário/aprovação para contornar esse controle.
- `vercel.json` prepara build do dashboard na raiz do monorepo.

## Validação local

- **409 passed, 2 skipped**, excluindo `test_migration_validator.py` por DLL asyncpg bloqueada pelo Windows. Depois foi acrescentado teste de cancelamento com release expirada: os **6 testes de recuperação/autorização passaram**.
- Build e TypeScript do frontend passaram (53 módulos); contratos Node passaram.
- Python 3.12 isolado em `work/audit312`; SQLAlchemy 2.0.44 sem extensão C contorna restrição local. Não alteramos pin do projeto para isso.
- PostgreSQL, Redis e migrações completas precisam de CI Linux. CI histórico de main não valida estas mudanças. Consultar o novo CI no PR desta branch.
- Sem deploy, teste ponta a ponta hospedado ou ordens enviadas à corretora nesta etapa.

## Infraestrutura e bloqueios observados

- Vercel: projeto `capital-cipher-ai`, id `prj_HACPEQ09CfOHhFimM1ZJtrlN3J6L`; deployment consultado pertence ao repositório legado **capital-cipher-ai**, não **capital-cipher-platform**. Não sobrescrever ou trocar vínculo silenciosamente.
- Supabase: `phkligpkcitbbefrrotk` / `capital-cipher-staging` consta ACTIVE_HEALTHY, mas consulta SQL falhou com ECONNREFUSED no IPv6/5432. Existência do projeto não comprova acesso ao banco. Legado `ocnsqdgkgpsuwimjolil` consta INACTIVE.
- Fly: configs existentes `capital-cipher-paper-staging` e `capital-cipher-bybit-testnet`; CLI local sem sessão/token. Estado atual e deploy não verificados.
- Runtime depende de processos contínuos de feed, workers e watchdog, além de Redis/banco. Publicar somente o painel Vercel não executa esse runtime.
- Credenciais Bybit TESTNET, banco/Redis isolados e permissões mínimas ainda precisam ser comprovados no host. Nunca colocar segredos em VITE, Git ou chat.

## Próximos passos obrigatórios

1. Revisar CI e diff deste PR; manter draft até cumprir os critérios de implantação.
2. Corrigir risco/contabilidade para usar equity, PnL e posições da conta TESTNET, em vez do saldo PAPER.
3. Implementar saídas protegidas, stop/take-profit, reduce-only, preenchimentos parciais e configuração/validação de alavancagem Bybit. Kill switch atual não equivale a zerar posições.
4. Alimentar/aquecer candles do mercado Bybit e timeframes usados; validar gaps, reconexão e recuperação.
5. Validar migrações em PostgreSQL descartável; resolver acesso Supabase e autenticação do host. Verificar isolamento TESTNET.
6. Obter evidências e atestação verdadeiras para o commit exato, aprovação persistida e canário limitado. Ensaio local existente não é canário remoto real.
7. Implantar backend compatível, configurar URL HTTPS e CORS do painel, testar navegador → API → banco, reinício, reconciliação e kill switch. Registrar URLs, commit e rollback sem segredos.
8. Reavaliar estratégias com modelo de custos e caminho efetivamente executado. Os cinco cenários OOS históricos inspecionados perderam dinheiro; não há evidência de lucratividade. Não otimizar usando o período reservado de teste.

## Retomar

Ler este arquivo e `docs/vercel-supabase-deployment.md`. Conferir branch, commits, PR e CI antes de editar. Continuar a preparação de **Bybit Futuros TESTNET** em **Vercel + Supabase**, com host compatível para runtime. Não afirmar conclusão sem deploy e fluxo completo comprovados. Correções já estão autorizadas pelo usuário.
