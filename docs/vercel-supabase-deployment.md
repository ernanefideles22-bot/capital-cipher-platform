# Painel Vercel e backend com Supabase

Estado: configuração preparada; implantação não executada e TESTNET não liberada.

## Painel

Vincular o repositório `ernanefideles22-bot/capital-cipher-platform`, diretório raiz, usando `vercel.json`. O projeto encontrado `capital-cipher-ai` pertence ao repositório legado; preservar até migração explícita.

Build: `pnpm install --frozen-lockfile`, depois `pnpm --filter @capital-cipher/dashboard build`; saída `frontend/dist`.

Definir antes do build `VITE_API_BASE_URL=https://<host-do-backend>/api/v1`. O valor é público. Nunca incluir chaves de administrador, segredos Supabase ou Bybit em variáveis VITE. Sem URL, o painel usa `/api/v1` na própria origem, adequado quando servido pelo backend. Alterar variável VITE exige novo build.

No backend, configurar `CORS_ALLOWED_ORIGINS` com as origens exatas autorizadas. Testar preview contra staging antes de promover.

## Runtime e dados

As configurações em `deploy/fly` e `deploy/testnet` são a base dos processos contínuos e watchdog. O deploy estático não executa esses processos.

Supabase armazena dados duráveis; Redis atende transporte/filas. TESTNET exige isolamento de PAPER, migrações versionadas e privilégios mínimos. Resolver conectividade SQL antes de migrar. Nunca validar migrações de teste em produção.

Na imagem Fly, usar `--build-arg APP_SOURCE_REVISION=<SHA-completo-do-commit>`; deve corresponder ao código construído e à decisão de release persistida. Sem isso, entradas TESTNET ficam bloqueadas. Não reutilizar SHA aprovado de código antigo.

## Para concluir

Exigir CI completo (PostgreSQL/Redis/migrações), correções pendentes de conta/proteções/feed, health/readiness do host, painel conectado, reinício recuperando estado, reconciliação/kill switch testados, liberação TESTNET válida, canário real limitado e rollback documentado. Consultar o backlog em `production-readiness-handoff.md`. Dinheiro real está fora do escopo.
