# Fase C — escopo de preparação e critérios verificáveis

A Fase B foi concluída e o acompanhamento PAPER está ativo. Este documento e
`backend/scripts/review_paper_readiness.py` preparam a Fase C. Não certificam
essa fase, não alteram o runtime nem autorizam publicação ou dinheiro real.

## Revisão do código existente

Referência revisada: `b4186abefa45fcb3cc4740988de12eed866546c7`; runtime certificado
`ec882651ac0a098891420d6159f0f9ba42a05f5b`. A árvore operacional relevante é a mesma,
acrescida do observador externo e documentação.

| Área | Implementação atual | Evidência ainda necessária na Fase C |
|---|---|---|
| Mesas | StrategyEngine seleciona configurações; nove assinaturas 15m/1h/4h | Três mesas concorrentes com mesma carteira, identidade estratégia/versão/ativo/candle e replay |
| Capital | RiskManager mantém um livro compartilhado; limites globais e de estratégia | Reservas pendentes, não apenas posições abertas, respeitam o saldo e os limites comuns |
| Concorrência | Orchestrator usa `_cycle_lock` no processo quando Office está ativo | Dois processos/conexões independentes concorrendo pela última capacidade no PostgreSQL |
| Consumo | Transação bloqueia a linha global `risk_control_state`, a aprovação e posições duráveis; revalida hash do livro | Teste PG17 com três aprovações distintas chegando simultaneamente ao consumo |
| Sinais vencidos | Verificação antes do risco e antes do envio pelo OMS | Expiração durante espera pelo lock, persistência lenta e fila; nenhuma ordem tardia |
| Recuperação | Fase B verificou reconstrução de estado e dependências | Crash entre reserva/consumo/envio, lease/replay e liberação segura da reserva |
| Latência | Percentis móveis de ciclo já existem | Evento, fila, decisão, risco, envio e fill medidos por relógio monotônico; amostra e custo publicados |

Fontes: `app/orchestrator/service.py`, `app/risk/manager.py`,
`app/risk/portfolio.py`, `app/paper_trading/engine.py`,
`app/database/repositories/repository.py` e `docs/ai-trading-office-phase-a.md`.
O PostgreSQL já tem uma guarda durável compartilhada: a migração cria a linha
singleton de controle, e o consumo usa essa linha como lock comum antes de
revalidar as posições. Não se presume ausência dessa proteção nem se propõe
substituí-la sem teste. O teste nativo novo usa três RiskManagers, três engines,
três pools/conexões independentes, aprovações distintas e uma barreira antes do
consumo. Verifica direções iguais/opostas, capacidade comum e restauração após
reinicialização. Seu resultado depende da execução PG17 no CI, não dos testes
unitários do relatório externo. Isso não executa o pipeline inteiro das mesas.

## Análise externa disponível agora

O novo script usa somente arquivos privados e biblioteca padrão Python. Não
faz HTTP, não recebe credenciais e não importa o código da aplicação.

```bash
python3 backend/scripts/review_paper_readiness.py \
  --snapshot /private/captura-paper.json \
  --risk-context /private/contexto-risco.json \
  --output /private/revisao-paper.json
```

O snapshot deve ser OBSERVING, com hash válido e revisão certificada. O contexto
opcional contém respostas públicas GET de `/risk/limits` e `/risk/status`, status
antes/depois iguais, origem/revisão iguais e término a até cinco minutos da
captura PAPER. Divergência do livro, hash, escopo ou amostra impede a análise.
Os arquivos de saída são exclusivos e não sobrescrevem baseline ou evidências.
Nenhuma captura privada deve ser adicionada ao repositório.

O livro usa notional de ordens FILLED abertas. Compra e venda opostas se
cancelam na exposição líquida, mas continuam somadas na exposição bruta. Uma
ordem parcialmente preenchida exige reconciliação própria; não se assume
execução total. Limites observados são configuração, não saldo disponível ou
instrução de dimensionamento. Reservas pendentes não aparecem nesse livro.

## Qualidade da amostra dos agentes

O relatório mostra amostra total e direcional, cobertura direcional, acurácia,
Brier e contribuição marginal. Uma acurácia de 83,33% com seis observações
direcionais corresponde a cinco acertos; não equivale a 147 tentativas de direção.

O intervalo de Wilson de 95% é descritivo. Não corrige seleção entre 300 agentes,
dependência entre previsões ou escolha após observar resultados. Não constitui
significância, causalidade financeira ou comparação fora da amostra.

A marca de 30 amostras direcionais é uma **proposta de triagem de pesquisa**
neste relatório externo. Não substitui o mínimo operacional existente, não
altera candidatos do runtime e não concede qualificação por regime. A pesquisa
ainda exige histórico causal suficiente por regime, custos, separação temporal,
holdout e validação da contribuição sem vazamento. Nenhum agente é promovido.

## Sequência de implementação delimitada

1. Congelar contratos e fixtures para mesas 15m/1h/4h, mesma carteira e identidade
   completa. Definir conflito por ativo/direção e semântica de reserva/redução
   explicitamente antes de mudar a política operacional.
2. Reutilizar e comprovar a guarda transacional existente. Delimitar se a fase
   exige reserva anterior ao consumo para aprovações pendentes; só então propor
   eventual contrato adicional de validade, identidade, consumo e reconciliação.
   Definir lock order e orçamento de conexões; nunca esperar agentes SHADOW
   segurando o lock.
3. Testar separadamente entradas concorrentes iguais/opostas, último slot/capital,
   limites globais mais restritos que a soma das mesas, duplicidade e sinais que
   vencem durante a espera. Nenhuma aprovação individual pode duplicar capital.
4. Testar PostgreSQL/Redis indisponíveis, crash e replay em ambiente descartável,
   preservando principal, backups e a campanha PAPER atual.
5. Medir latência real por etapa, executar CI integral e apresentar commit exato,
   migrações, configuração, impacto, limites e rollback para revisão da publicação.

Critério de encerramento: todos esses cenários com evidência reproduzível e
runtime PAPER publicado e verificado. Número de dias, quantidade de agentes,
apenas percentis de ciclo ou CI isolado não encerram a Fase C nem provam lucro.
