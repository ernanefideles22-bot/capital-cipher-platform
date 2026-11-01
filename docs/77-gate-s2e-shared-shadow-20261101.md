# Capital Cipher — Etapa 2E: SHADOW de confluência compartilhando dados da Etapa 1

**Status: implementação em branch separada; não publicada no Railway.** PR depende de PRs #75 e #76. Serviço ativo continua no commit `2eb009b`.

## Credenciais preexistentes

Verificado em Railway staging: existem os nomes das variáveis `CAPITAL_CIPHER_GATE_TESTNET_API_KEY` e `CAPITAL_CIPHER_GATE_TESTNET_API_SECRET`, com valores ocultados pelo conector. Isso confirma **configuração**, não validade de assinatura ou autorização de trading. A rotina `gate_service.refresh` já contém uma checagem autenticada *read-only* e os monitores de inventário. Esta etapa não chama essas funções, não recebe nem lê credenciais, e não tenta enviar ordens.

## Módulo S2E: mesmo conjunto de candles, uma única coleta

- `GATE_S2E_SHADOW_ENABLED` é uma **nova variável opt-in**, *ausente* em staging e considerada `false` por padrão.
- A Etapa 2E só pode iniciar se **ambas** `GATE_S1_SHADOW_ENABLED=true` e `GATE_S2E_SHADOW_ENABLED=true` estiverem configuradas, depois de revisão e autorização de deploy.
- A etapa atual coleta `200` candles fechados da Gate USDT Futures Testnet para 15m/1h/4h, em BTC/ETH/SOL, usando **apenas GET público**. `professional_shadow_snapshot` recebe um hook opcional S2E **após** validar o resultado S1, e entrega cópias dos mesmos candles e o horário de observação real. Não duplica consultas HTTP nem cria leitura privada.
- S2E calcula a leitura profissional S1, as propostas das três estratégias S2 e o filtro causal de confluência `gate_s2c_confluence.evaluate_confluence`. S2E produz pesquisa `HYPOTHESIS_ONLY`, `FILTERED` ou `NO_PROPOSAL`, sem persistência, sem alteração de stops, sem OMS, banco, audit, volumes, registro dos 300 agentes ou fila de ordens.
- Um erro no hook secundário gera somente `strategy_shadow.status=BLOCKED` no resultado; `professional_shadow.status` permanece independente da Etapa 2E. Mensagens de exceções privadas não são propagadas, apenas nomes de classe.
- A resposta `/api/gate/status` ganha o objeto `strategy_shadow`, `DISABLED` por padrão, e logs contêm apenas contagens agregadas `GATE_S2E_SHADOW_STATUS ... execution=false`.
- Não reutilizar `GATE_OMS_CONNECTION_ENABLED`, `GATE_OMS_WRITES_ENABLED` nem chaves da Gate para pesquisa. Não presumir que a simples existência dessas variáveis indique valores `false`; o conector não forneceu seus valores.

## Limites de evidência

A integração melhora a observabilidade e troca de dados entre Etapas 1 e 2; **não melhora automaticamente a lucratividade**. A comparação histórica com a confluência (PR #75) apresentou perdas líquidas e não pode ser usada como prova de edge: os mesmos candles já foram inspecionados. O S2D (PR #76) pré-congelou o início dos novos candles em 2026-11-02T00:00:00Z, requer dados posteriores, histórico pré-freeze e comparação com baseline e com não operar.

Não habilitar flag no Railway nem promover para ordem sem revisão independente da integridade do serviço, logs, memória, CI geral e liberação explícita do proprietário. **Teste S2E somente em staging SHADOW, nunca TESTNET order ou LIVE**.
