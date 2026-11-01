# Capital Cipher — Runbook de liberação S1+S2E SHADOW e rollback do Railway staging

**Documento de preparação, não autorização para publicar.** Elaborado em 2026-11-01 usando inspeção somente leitura do Railway e do GitHub. Nenhum deploy, alteração de volume ou migração é executado por este documento.

## Identidade do serviço e estado confirmado

| Item | Valor confirmado |
|---|---|
| Railway project | `b21708b8-5703-49f0-9cbd-9ff74be94ce8` |
| Environment | `2f8f48c0-ed89-4ce4-bbb6-50df36a62680` (**staging**) |
| Gate service | `c75db2ba-251b-4af9-97b5-a05b9b7da4ea` (`capital-cipher-gate-testnet`) |
| Deployment da Etapa 1 efetivamente ativo | `bd4b1ffb-727a-4582-9e3e-ad6fa53b9767` — `SUCCESS` |
| Commit efetivamente ativo | `2eb009ba161ce42005de462e343901de5ae2abfd` |
| Branch de origem efetivamente ativa | `feature/gate-pro-trader-s1-shadow-integration-20261101` |
| Patch staged para publicação | 3 alterações, não destrutivas, **ainda não aceitas** |
| Commit staged da integração S1+S2E | `8f62ed079b73c1d89e570a3ff7b9be45c1d6a58f` |
| Branch staged | `feature/gate-s2e-shared-s1-s2-shadow-optin-20261101` |
| Flag staged nova | `GATE_S2E_SHADOW_ENABLED=true` |
| Volume existente | `capital-cipher-data` montado em `/gate-data` |
| Registro de ordens TESTNET/LIVE | **não autorizado a ser ativado** |

O endpoint `describe_service` projeta a configuração **com alterações staged aplicadas** e pode exibir o commit novo mesmo quando ele **ainda não está em execução**. A prova do código *realmente implantado* é `list_deployments.meta.commitHash`, em combinação com logs e o deployment em `SUCCESS`. Não usar a configuração projetada como prova de liberação.

PRs relacionados: #77 (S1+S2E), #78 (scorecard S3), #79 (persistência S3). **Publicar #77 não significa instalar/ativar #78 ou #79**; os últimos continuam somente pesquisa não integrada.

## Gate 0 — antes do deployment, sem atalhos

1. **Auditoria de estabilidade independente da Etapa 1.** Deployment S1 iniciou em `2026-11-01T08:02:17Z` (04h02 MT). Auditoria agendada `2026-11-02T08:20Z` (04h20 MT). Atestar **pelo menos 24 horas de dados reais** desde o início da observação S1; comparar primeiro e último timestamps dos ciclos, não usar apenas a contagem. Reprovar em caso de lacunas relevantes, resets inexplicados, `PARTIAL_OR_BLOCKED`, `GATE_S1_SHADOW_MONITOR_BLOCKED` ou comportamento incorreto. Uma única avaliação `SUCCESS` não homologa 24h.
2. **Conectividade e segurança.** Confirmar 3/3 BTC/ETH/SOL, logs recentes de leitura, inventário de ordens/posições e recuperação de ciclos `CLEAR`. Confirmar que ordens TESTNET/LIVE não estão habilitadas. Os valores das variáveis podem estar ocultos pelo conector; **não presumir valores de flag por existir o nome**. Não imprimir chaves, secrets, token ou saldos. Se não houver evidência do estado de execução, suspender a liberação.
3. **Integridade do patch.** `get_staged_changes` deve mostrar **exatamente** três alterações para o serviço: `source.branch`, `source.commitSha` e `variables.GATE_S2E_SHADOW_ENABLED.encryptedValue`. Nenhuma remoção/criação de serviço, volume, banco ou mudanças compartilhadas. Se houver outras alterações staged no mesmo ambiente, **NÃO aceitar o patch coletivo** antes de revisão.
4. **Qualidade.** CI geral e específico do PR #77 em `SUCCESS` no commit pinned `8f62ed0`. O código da Etapa 1 permanece read-only; a Etapa 2E só lê os candles da S1. Testes do PR #79 não autorizam sua implantação.
5. **Aprovação.** Deployment explícito e separado de qualquer autorização de ordens. A conclusão da auditoria **não executa deploy automaticamente**. Obter confirmação de liberação do operador, observando janela e risco de reinicialização. Não trocar o banco nem criar serviço pago.

## Gate 1 — deployment SHADOW S1+S2E (somente após aprovação explícita)

Antes de executar qualquer ferramenta de escrita:
- Ler novamente `get_staged_changes`, `list_deployments`, `describe_service`; verificar ausência de outra alteração de usuário, patch `STAGED` não `APPLYING`, CI aprovado, S1 homologada.
- Confirmar que a configuração de ordem `GATE_OMS_WRITES_ENABLED` **não será alterada**. Não mexer em `GATE_OMS_CONNECTION_ENABLED`, chaves Gate ou `GATE_DEMO_OWNER_GRANT_JSON`.
- Apenas após autorização expressa do operador: usar **`accept_deploy`** no ambiente staging **uma vez**. Registrar `workflowId`, novo deployment ID e SHA realmente publicado. Esse passo reinicia o monitor e invalida uma contagem de ciclos ininterruptos iniciada na Etapa 1; não fazê-lo antes da auditoria.
- Aguardar o resultado de build/deploy, consultar logs e `/health`, `/api/gate/status` **somente por acesso aprovado e sem expor dados privados**. Não inferir sucesso apenas pela aceitação do patch.

## Gate 2 — confirmação após publicar

Um release bom precisa de:
- `SUCCESS` do novo deployment no commit `8f62ed0` e ausência de crash/restart loop.
- `professional_shadow.status=AVAILABLE` com 3/3 mercados, `execution_authorized=false` e monitor S1 com tempos/candles válidos.
- `strategy_shadow.status=AVAILABLE` com três mercados `READ_ONLY_CONFLUENCE_READY`, políticas corretas, `execution_authorized=false` e sinais de S2E classificados exclusivamente como pesquisa. Um mercado bloqueado faz o rollout **parcial** e exige análise; não considerar S2E homologada.
- Inexistência de ordens/posições inesperadas e de escrita executável; estado de OMS central/risco não deve ganhar autorização. Garantir `/gate-data` íntegro e montado. Não expor chaves em logs ou APIs públicas.
- Salvar contagens por ciclo de candidatos, WAIT, filtros, erros, latência, divergências entre estratégias; verificar continuação por múltiplos ciclos antes de declarar estabilidade. **Não inferir lucratividade**; nenhum PnL prospectivo S3 é gravado nessa release.

## Rollback controlado (caso haja falha)

**Objetivo:** voltar para código S1 conhecido e desligar o hook S2E preservando volumes e credenciais.

1. Isolar o sintoma: guardar deployment ID, falhas de saúde/latência, `strategy_shadow` e `professional_shadow`, status de OMS e qualquer ciclo não resolvido. Se houver ordem inesperada ou risco, interromper novas liberações e seguir o procedimento de risco/OMS; **não excluir registros para limpar o problema**.
2. Para reversão por ferramenta, o Railway permite `redeploy` de um deployment antigo (deploy `bd4b1ffb-727a-4582-9e3e-ad6fa53b9767`), mas isso **sozinho não prova que o SOURCE/FLAGS staged ou atuais foram revertidos**. Planejar explicitamente restaurar `source.branch=feature/gate-pro-trader-s1-shadow-integration-20261101`, `source.commitSha=2eb009ba161ce42005de462e343901de5ae2abfd`, e `GATE_S2E_SHADOW_ENABLED=false` no **mesmo staging**. Essas operações são **outro deployment**, exigem aprovação, revisão de patch e supervisão.
3. Para restabelecimento imediato após incidente, sob autorização expressa, a operação `redeploy` da build antiga é uma opção provisória; depois conferir/retornar source e flag para evitar que uma implantação futura volte inadvertidamente à nova versão.
4. Revisar `get_staged_changes` de novo antes de cada `accept_deploy`; esse comando aplica **todas** as alterações pendentes do ambiente, não apenas as desejadas. Rejeitar alterações destrutivas e qualquer mudança imprevista. Não tocar no volume `capital-cipher-data`, `cycles.sqlite3`, `research.sqlite3`, Postgres, Redis, OMS, chaves ou 300 agentes.
5. Revalidar `SUCCESS` da SHA antiga, `/health`, execução sem ordens e S1 3/3. O rollback também reinicia o monitor: relatar que a sequência de disponibilidade mudou e **não reutilizar as métricas de estabilidade como se fossem ininterruptas**.

## Decisão de próxima fase

S3B tem testes dedicados e CI geral de código, mas sua nova base SQLite **não foi montada nem executada** no Railway; antes de conectar S3B ao SHADOW, exigir nova revisão de separação de arquivos, capacidade de disco, WAL/restart, concorrência, timestamp real de receipt e retenção com recuperação. Preferir rollout sequencial: S1 homologada -> S2E SHADOW comprovado -> S3 passivo com registro durável -> resultados prospectivos suficientes -> decisões futuras de execução **sempre separadamente autorizadas**.
