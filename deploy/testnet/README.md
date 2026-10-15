# Bybit TESTNET isolado

Este diretório é a configuração separada do ambiente Bybit V5 TESTNET. O
ambiente PAPER existente (`capital-cipher-paper-staging`) não é alterado por
esses arquivos.

## Limites de segurança

- `SYSTEM_MODE` continua sendo `PAPER`; somente o OMS autenticado pode acessar
  a Bybit TESTNET.
- O endpoint é fixo em `https://api-testnet.bybit.com`, categoria `linear`.
- As chaves `CAPITAL_CIPHER_BYBIT_TESTNET_*` são lidas somente do ambiente e
  nunca devem ser gravadas no Git, em `.env` versionado ou no chat.
- O preflight exige PostgreSQL e Redis próprios, com IDs de recurso diferentes
  dos usados pelo staging. SQLite, hosts locais/compartilhados e a base PAPER
  são rejeitados no ambiente hospedado.
- O preflight não faz chamadas de rede. O boot da aplicação ainda verifica as
  migrações Month 7 e faz healthcheck autenticado da Bybit antes de iniciar os
  workers.

## Rehearsal local

Use um `.env` local não versionado contendo, no mínimo:

```text
TESTNET_POSTGRES_PASSWORD=<segredo forte>
TESTNET_REDIS_PASSWORD=<segredo forte>
ADMIN_API_KEY=<segredo com pelo menos 32 caracteres>
CAPITAL_CIPHER_BYBIT_TESTNET_KEY_ID=<chave da Bybit TESTNET>
CAPITAL_CIPHER_BYBIT_TESTNET_SIGNING_SECRET=<segredo da Bybit TESTNET>
```

Depois, a partir de `deploy/testnet`:

```text
docker compose --env-file .env -f compose.yml up --build
```

O backend local fica em `http://127.0.0.1:8001`. Para parar sem apagar os
volumes isolados, use `docker compose -f compose.yml down`. Não use `down -v`
até ter certeza de que o histórico local pode ser descartado.

## Fly.io/Supabase

`../fly/fly-testnet.toml` está pronto para revisão, mas não provisiona recursos
por si só. Antes de qualquer deploy, é necessário:

1. Criar um projeto Supabase/PostgreSQL separado para TESTNET e aplicar todas
   as migrações do diretório `supabase/migrations`.
2. Criar Redis/Upstash separado, com TLS, e registrar o hostname esperado.
3. Definir `TESTNET_DATABASE_RESOURCE_ID` e `TESTNET_REDIS_RESOURCE_ID` como
   metadados não secretos e guardar `DATABASE_URL`, `REDIS_URL`,
   `ADMIN_API_KEY`, o certificado CA e as duas chaves Bybit somente no secret
   store do Fly.
4. Confirmar o orçamento mensal antes de criar o app/volume/máquina.

Nenhum desses recursos pagos é criado automaticamente por este commit.
