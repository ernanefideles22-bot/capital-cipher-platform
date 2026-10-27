# Fase B — complemento de certificação operacional

Este complemento parte do candidato `7f66a8d` do PR #40 e preserva seu código
operacional. Altera somente validadores, benchmarks, testes e documentação.
O backend publicado de referência permanece em `044839f`. A Fase C não começa
e LIVE/TESTNET continuam desabilitados.

## Lacuna corrigida no validador

O fluxo completo de risco, OMS, ordem PAPER, auditoria e reinício estava fixado
em BTCUSDT/15m. Agora aceita explicitamente `15m`, `1h` e `4h`, verifica a versão
da estratégia selecionada e espaça o histórico sintético pelo intervalo correto.
As validações de frescor operacionais não são alteradas. Timeframes não suportados
são recusados antes de construir a aplicação ou abrir o banco.

O teste de agente CPU-bound comprova que o loop infinito foi iniciado antes da
segunda decisão e roda nos três timeframes. Outro teste entrega simultaneamente
os três streams, com quatro repetições de cada candle e limite de uma posição:
exige uma decisão por stream e uma única ordem aprovada pelo risco/OMS.

Os testes nativos PostgreSQL 17/Redis incluem BUY e SELL nos três timeframes,
tanto no caminho anterior quanto no assíncrono. Esses testes só podem usar
servidores locais descartáveis: o guard contra hosts remotos permanece.

## Comandos reproduzíveis

```bash
cd backend
python -m pytest app/tests -q -ra
python -m app.validation.office_latency --samples 40 --timeframe 15m --output latency-15m.json
python -m app.validation.office_latency --samples 40 --timeframe 1h --output latency-1h.json
python -m app.validation.office_latency --samples 40 --timeframe 4h --output latency-4h.json
```

Executar os benchmarks sequencialmente, sem concorrência com a regressão. Cada
amostra usa os 300 agentes existentes e valida risco, OMS e ordem PAPER; o contexto
analítico termina separadamente. O atraso SHADOW de 100 ms é controlado. Banco
durável e rede Railway não participam desse benchmark. P99 com 40 amostras é
exploratório e não certifica SLO hospedado nem desempenho financeiro.

## Escopo de uma futura execução hospedada

O job preserva a configuração anterior por padrão: `PAPER_VALIDATION_TIMEFRAMES`
ausente executa apenas 15m. O valor explícito `15m,1h,4h` habilita seis fluxos
BUY/SELL nas mesmas cópias isoladas da revisão, seguido das proteções existentes.
Valores desconhecidos, repetidos ou malformados falham antes de qualquer conexão.
Essa opção não habilita estratégias no backend nem executa o job por si só.

Qualquer execução hospedada deve ser revisada e aprovada com revisão exata,
região, orçamento de conexões, duração e espaço disponível. As cópias anteriores
não podem ser reaproveitadas ou apagadas. O pacote de recuperação integral deve
estar guardado fora do container antes de reiniciar ou mover o PostgreSQL.

## Critérios ainda necessários para encerrar a Fase B

1. Concluir a retirada e retenção durável do backup completo já restaurado.
2. Resolver e aprovar a topologia de backend/banco, sem relaxar deadlines.
3. Aprovar a publicação controlada de uma revisão com CI completo.
4. Medir o runtime final com dados e amostra explicitamente identificados; provar
   isolamento SHADOW, integridade, recuperação, falhas e as três estratégias.

Código testado localmente ou no CI não é declaração de certificação no staging.
