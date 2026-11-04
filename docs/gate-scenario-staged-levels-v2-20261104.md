# Níveis e gatilhos por etapas — hipótese V2

## Problema e escopo

O observador V1 congela o gatilho antes do teste da região. Isso é diferente de
identificar o último fundo/topo local disponível depois de uma rejeição. A
medição anterior registrou 145 planos, um rompimento e zero entradas; seu pacote
bruto ficou incompleto. Essa evidência não permite atribuir todos os descartes
ao gatilho, nem concluir que a V2 será mais frequente ou lucrativa.

Esta é uma hipótese técnica nova, posterior ao resultado V1. Não é correção
retroativa daquela medição. O código V1, seu diário e seu replay financeiro
permanecem inalterados. A V2 é acessível somente por CLI de pesquisa; não está
ligada ao scheduler, aos agentes implantados, ao OMS ou à corretora.

## Contrato determinístico

1. Usar a leitura S1 existente de candles fechados/recebidos de 4h, 1h e 15m.
   Manter exatamente o filtro direcional V1. Padrões, linhas e Fibonacci
   continuam como anotações, sem votos ou pontuação de confiança inventados.
2. Antes do toque, congelar somente a região de suporte/resistência de 15m,
   seu pivô, o buffer e o prazo. A seleção por distância e os parâmetros são
   os mesmos da V1; empates usam o pivô mais recente.
3. Exigir toque em candle de 15m posterior ao plano e rejeição em OUTRO candle
   de 15m. Stop: extremo acumulado do toque/rejeição mais buffer V1. Congelar
   esse stop na rejeição; nunca alargá-lo depois.
4. Depois da rejeição, escolher o pivô oposto mais recente de 15m cujo horário
   esteja depois da âncora da região e até o fechamento da rejeição, inclusive.
   Ele precisa estar confirmado/recebido no instante da decisão. A ocorrência
   pode preceder o plano: a confirmação posterior, não um indicador com futuro,
   é o que torna esse pivô recém-disponível para seleção.
5. Se nenhum pivô dessa perna estiver confirmado, aguardar. Pivôs formados
   depois da rejeição não serão usados para perseguir outra perna. Se um
   fechamento de 15m desde a rejeição já alcançou/cruzou o gatilho antes de ele
   ser congelado, invalidar. Não recorrer a um pivô antigo mais conveniente.
6. Alvo: PRIMEIRO obstáculo estrutural além do gatilho, considerando pivôs dos
   três períodos. Não pular um obstáculo de 15m para escolher um alvo distante.
   Invalidar alvo já tocado desde a rejeição. Exigir razão líquida >=2 já na
   montagem, calculada no gatilho como referência otimista, jamais como fill.
7. Congelar gatilho, alvo e stop numa segunda identidade vinculada à região e
   à rejeição. Exigir rompimento por fechamento de OUTRO candle de 15m, recebido
   depois desse congelamento. Não usar a barra de seleção como rompimento.
8. Exigir outro candle de 1m fechado e recebido depois do sinal, em até dois
   minutos. Recalcular a razão líquida no preço dessa reconfirmação. Volta ao
   gatilho, falta de espaço, toque em stop/alvo, mudança de contexto, prazo
   vencido ou lacunas impedem proposta.

Taxa 5 bp/lado e slippage nominal 6 bp/lado continuam como hipóteses, sem
consulta à conta pessoal. Região 0,2 ATR, buffer 0,1 ATR e validade de oito
candles de 15m não foram afrouxados. A V2 também recusa toque no alvo por pavio
antes da proposta, incluindo na barra do rompimento. Ambiguidade de stop na
barra não recebe interpretação favorável. Cada candidato continua sem
quantidade executável e com `execution_authorized=false`.

## Estado, uso e preservação

Fluxo: WAIT_TOUCH → WAIT_REJECTION → WAIT_LOCAL_PIVOT (quando necessário) →
WAIT_BREAK → WAIT_RECHECK → CANDIDATE_ONLY, INVALIDATED ou EXPIRED.

`gate_scenario_staged_journal.py` usa `scenario-staged-levels.sqlite3`, separado
do diário V1. Mantém snapshots completos, encadeamento de hashes, fingerprint
das fontes, bloqueio em falhas e retry idempotente. Revisão é somente leitura.
Não há migração/reset de estudo anterior. Os relógios dos snapshots fornecidos
não são atestados externamente.

```bash
cd backend
python -m scripts.gate_scenario_staged_levels analyze --snapshot /dados/snapshot.json --output /dados/analise-v2.json
python -m scripts.gate_scenario_staged_levels register --snapshot /dados/snapshot.json --root /dados/sessao-v2 --output /dados/registro-v2.json
python -m scripts.gate_scenario_staged_levels observe --snapshot /dados/proximo.json --root /dados/sessao-v2 --output /dados/observacao-v2.json
python -m scripts.gate_scenario_staged_levels review --root /dados/sessao-v2 --output /dados/revisao-v2.json
python -m scripts.gate_scenario_staged_demo --root /dados/demo-v2-nova
```

Os exemplos usam saídas novas; uma saída existente é recusada antes de mutar
o diário. A demonstração usa preços inventados, não dados de mercado, lucro ou
nova amostra financeira. A CI preserva os diários de engenharia e o resultado
JUnit por 30 dias. Isso não substitui preservação durável de um estudo financeiro.

## Limite deste avanço e próximo estudo

Esta entrega verifica a montagem temporal dos níveis e suas recusas. Não
executa novo backtest de lucratividade, não seleciona janelas/parâmetros por
resultado e não acessa a reserva existente. Candidatos sintéticos não contam
como operações históricas.

Verificação local: **132 testes direcionados passaram**, incluindo 36 novos,
em 36,07 segundos (um aviso de dependência). Inclui o caso de pivô recém-confirmado,
reconfirmação separada, custos, alvo próximo, dados futuros/lacunas, restart,
concorrência, corrupção e coexistência sem reescrever o diário V1.

Antes de outra medição: preservar dados e protocolo de forma durável ANTES do
run; fixar código, fontes, janelas, hipóteses de execução e limite de variantes;
registrar V2 como hipótese separada; medir o funil por motivo, operações
encerradas e resultado após custos. Não alterar o filtro para conseguir trades
nem tratar repetição da amostra V1 como confirmação independente. A hipótese
testável é melhoria na escolha causal do último pivô, não uma promessa de lucro.
