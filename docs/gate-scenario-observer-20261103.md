# Leitura técnica com cenário, espera e reconfirmação

Esta entrega implementa um observador de cenários de pesquisa. Reutiliza o
leitor técnico S1 existente, fixa os níveis antes de um futuro toque e registra
as etapas seguintes. Não é uma estratégia com vantagem demonstrada, um novo
backtest, aprendizado autônomo ou uma autorização de operação.

O problema tratado é concreto: uma leitura isolada pode dizer “esperar rejeitar
a resistência e romper o fundo”, sem conservar qual resistência/fundo foi
definido, se a rejeição aconteceu antes do rompimento e se a situação mudou
antes da proposta. O novo observador guarda esse contrato e sua sequência.

## Leitura e marcações

O adaptador exige o mesmo mercado GATE em 4h, 1h e 15m, com 80–3000 candles
fechados e recebidos até o horário da observação. O S1 valida continuidade,
OHLCV, timestamps e idade da última barra. O observador adiciona um candle de
1 minuto fechado/recebido, com idade máxima de dois minutos.

São expostos por período: regime, estrutura, padrões de candles, padrões
gráficos, suporte/resistência, os dois últimos pivôs confirmados para ancorar
cada linha, Fibonacci, ATR, volume relativo, RSI e MACD. São os resultados
descritivos reais do S1; não são probabilidades de acerto. Duas âncoras de linha
não são uma previsão do caminho futuro. Indicadores correlacionados não contam
como votos independentes e não é obrigatório que todos participem da entrada.

O primeiro contrato é simétrico para compra/venda:

- Contexto: estrutura e regime de tendência em 4h, sem tendência oposta em 1h.
- Região: pivô confirmado de 15m à frente do preço, com faixa de ±0,2 ATR.
- Gatilho: último pivô confirmado de 15m na direção do rompimento esperado.
- Alvo estrutural: próximo pivô confirmado além do gatilho, considerando os
  três períodos. Não inventa um alvo distante para satisfazer risco/retorno.
- Prazo: oito barras de 15m. Níveis, política e evidência inicial ficam congelados.

Esses parâmetros são definições iniciais de engenharia, sem otimização ou
validação financeira. Se não houver contexto, nível, gatilho e alvo coerentes,
o resultado é WAIT; não força uma leitura ou ordem.

## Sequência verificável

| Estado | Condição e próximo passo |
|---|---|
| WAIT_TOUCH | Aguarda uma nova barra fechada de 15m tocar a região congelada. |
| WAIT_REJECTION | Aguarda outra barra de 15m fechar para fora da região no sentido esperado. |
| WAIT_BREAK | Congela stop além do extremo do teste/rejeição, com 0,1 ATR de folga, e espera outro fechamento romper o pivô marcado. |
| WAIT_RECHECK | O rompimento fechado não é uma entrada: aguarda um novo candle de 1m, posterior ao gatilho e recebido depois da observação que o registrou. |
| CANDIDATE_ONLY | Contexto, manutenção do rompimento e espaço após taxas/slippage passaram. Emite uma proposta de pesquisa com identidade estável, sem quantidade ou ordem. |
| INVALIDATED / EXPIRED | Cancela e preserva o cenário; não altera níveis nem rearma automaticamente. |

O stop deve estar além do extremo observado, não simplesmente numa distância
fixa escolhida para aumentar a posição. Toque e rompimento no mesmo candle não
comprovam a ordem intrabar; o cenário é cancelado. Stop e gatilho na mesma barra
também não recebem interpretação favorável.

Cada minuto recebido revalida o contexto e pode invalidar a região/stop mesmo
antes de um novo fechamento de 15m. Há uma indicação de aproximação da região;
isso não dispara a proposta. Não sabemos antecipadamente a hora em que o preço
tocará a marcação. Portanto, “confirmar minutos antes” significa acompanhar a
aproximação e reconfirmar imediatamente antes da proposta, não prever o minuto
exato da entrada.

A confirmação posterior ao gatilho tem prazo de dois minutos. Se o preço voltar
para dentro do rompimento, alcançar o alvo antes da proposta ou perder espaço
econômico suficiente, o cenário é cancelado. Uma proposta consumida não gera
nova proposta a cada observação: o journal conta somente a transição inicial e
conserva um proposal_id estável.

## Custos e limites econômicos

Na reconfirmação são aplicadas, como hipóteses de planejamento, tarifa de
futuros de 5 bp por lado e slippage adverso de 6 bp por lado, tanto no alvo
quanto no stop. A relação de ganho/perda após esses custos precisa ser >=2.
Slippage compõe os preços e não é subtraído uma segunda vez.

Não há consulta de taxa pessoal autenticada. Funding, liquidação, margem,
liquidez, tamanho mínimo, ticks de preço, preenchimento, stops/saídas e limites
do risco operacional ainda precisam ser tratados antes de qualquer execução.
Os campos de saída explicitam essas pendências. Nenhum tamanho de posição é
calculado e não existe integração com broker, OMS, cadastro dos agentes ou
motor de risco. CANDIDATE_ONLY não significa operação elegível para execução.

O observador não acompanha posição aberta nem atribui PnL. Ausência de emoção
não cria informação preditiva. Precisão em seguir a sequência precisa ser
comprovada separadamente da lucratividade da regra.

## Memória e evidência

`scenario-observer.sqlite3` é um diário separado. `register` usa criação
exclusiva e registra plano, snapshot, política, estado e fingerprint das fontes
S1/observador/journal antes dos snapshots seguintes. O horário real de registro
é separado do horário de mercado fornecido. Receipts fornecidos por arquivo
não são atestação externa de observação prospectiva.

`observe` usa transação BEGIN IMMEDIATE. Cada evento possui hash encadeado;
UPDATE/DELETE são bloqueados. Repetição exata não acrescenta evento. O histórico
fechado sobreposto não pode mudar, inclusive seus recibos. Um adaptador futuro
precisa preservar o primeiro receipt de cada candle em cache.

Há limite de 256 observações por sessão e 512 KiB por snapshot. Falta de minuto,
dados futuros/alterados, perda de continuidade ou outra falha de validação é
registrada como REJECTED_INPUT e bloqueia a sessão. Não há reset nem retry que
apague a falha. O leitor exige a mesma versão do código; mudanças pedem revisão
e outra sessão, preservando a anterior. Hashes não impedem um administrador de
reescrever todas as evidências; não constituem atestação externa.

Reabrir o diário restaura o estado; retomar depois de um intervalo não observado
não inventa candles faltantes. O operador deve revisar a continuidade. Não há
varredura automática ilimitada para criar novos cenários depois de cancelamentos.

## Uso

Instalar as dependências do backend. O snapshot JSON contém `as_of`, `candles`
com arrays Candle de 4h/1h/15m e `minute` com um Candle de 1m. A demonstração
fornece exemplos completos sem precisar de rede, chaves ou infraestrutura.

```bash
cd backend
python -m scripts.gate_scenario_observer analyze --snapshot snapshot.json --output leitura.json
python -m scripts.gate_scenario_observer register --snapshot snapshot.json --root /analysis/cenario-001 --output registro.json
python -m scripts.gate_scenario_observer observe --snapshot proximo-minuto.json --root /analysis/cenario-001 --output observacao-001.json
python -m scripts.gate_scenario_observer review --root /analysis/cenario-001 --output revisao.json
python -m scripts.gate_scenario_demo --root /analysis/demonstracao-nova
```

Saídas e diretórios de registro devem ser novos; nenhum overwrite é implícito.
Use apenas um novo minuto por atualização, inclusive entre decisões de 15m.

## Verificação

Os testes exercitam a sequência simétrica, espera sem condições, invalidação
de contexto, rompimento precoce, barras ambíguas, atraso, perda do rompimento na
reconfirmação, pouco espaço após custos, níveis alterados, pivô futuro,
continuidade de minuto, persistência, retry concorrente, corrupção e reabertura
por outro processo. O teste integrado usa o leitor S1 real sobre OHLCV sintético,
sem substituí-lo por uma aprovação fictícia do padrão.

A demonstração tem seis caminhos inventados: compra e venda, cada uma com
confirmação válida, confirmação que falha e invalidação da região. Os casos
válidos terminam em CANDIDATE_ONLY, com uma proposta; os outros terminam em
INVALIDATED, sem proposta. São provas de comportamento de software, não seis
operações financeiras ou evidência de retorno.

Esta entrega não modifica a lógica S1/S2/S3 implantada ou resultados de pesquisa
anteriores. O observador está disponível por CLI e não está conectado ao serviço
Railway. Ainda faltam adaptador contínuo para os dados reais de 1m, tratamento
operacional de falhas/retomada e sua validação antes de uma implantação em
observação. Qualquer operação continua separadamente bloqueada.

A auditoria consolidada do histórico S4 e o registro global de tentativas
continuam trabalhos separados. Nenhuma amostra histórica ou reserva foi aberta
para medir desempenho desta nova regra.
