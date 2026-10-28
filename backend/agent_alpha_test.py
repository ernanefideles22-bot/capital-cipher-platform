"""Teste de poder preditivo dos agentes do capital-cipher-platform.

Pergunta: algum dos agentes direcionais tem vantagem estatistica REAL, fora da
amostra, depois de custos e depois de corrigir para multiplos testes?

Metodo
------
1. Alimenta os candles um a um em um CandleStore (so passado, sem look-ahead).
2. A cada `stride` barras roda todos os agentes e converte o sinal em posicao
   (BUY=+1, SELL=-1, resto=0), decidida no fechamento da barra t.
3. A posicao e mantida de open[t+1] ate close[t+stride] (execucao na barra
   seguinte, nunca no mesmo preco que gerou o sinal).
4. Custo por unidade de posicao trocada (taxa + slippage, por lado).
5. Selecao SO nos primeiros 60% (in-sample) com FDR de Benjamini-Hochberg;
   os sobreviventes sao avaliados nos 40% finais (out-of-sample), que nunca
   participaram da selecao.
6. Mede tambem quantos "agentes independentes" existem de verdade
   (participation ratio dos autovalores da correlacao entre posicoes).

Modos: synthetic-null (passeio aleatorio: nenhum agente deve sobreviver),
synthetic-ar (retornos com momentum embutido: controle positivo) e binance
(arquivos .zip/.csv de klines do data.binance.vision).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.agents.advanced_specialists import build_advanced_shadow_specialists
from app.agents.evaluation import SpecialistEvidenceService
from app.agents.month8_specialists import build_month8_shadow_specialists
from app.agents.month9_specialists import build_month9_shadow_specialists
from app.agents.month10_specialists import build_month10_shadow_specialists
from app.agents.month11_specialists import build_month11_shadow_specialists
from app.agents.quant import QuantAgent
from app.agents.specialists import build_shadow_specialists
from app.agents.trend import TrendAgent
from app.market_data.store import CandleStore
from app.schemas.agents import AgentInput
from app.schemas.common import AgentStatus, Signal
from app.schemas.market import Candle

TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
WARMUP = 250


# ----------------------------------------------------------------- dados
def _mk_candles(symbol, tf, opens, highs, lows, closes, vols, start):
    step = timedelta(minutes=TF_MINUTES[tf])
    out = []
    for i in range(len(closes)):
        out.append(
            Candle(
                exchange="BINANCE", symbol=symbol, timeframe=tf,
                open=float(opens[i]), high=float(highs[i]), low=float(lows[i]),
                close=float(closes[i]), volume=float(vols[i]),
                closed_at=start + step * (i + 1),
            )
        )
    return out


def synthetic(kind: str, n: int, symbol: str, tf: str, seed: int, phi: float):
    rng = np.random.default_rng(seed)
    vol = 0.004 * np.exp(0.5 * np.cumsum(rng.normal(0, 0.05, n)) / math.sqrt(50))
    eps = rng.normal(0, 1, n) * vol
    ret = np.zeros(n)
    for i in range(n):
        prev = ret[i - 1] if (kind == "ar" and i > 0) else 0.0
        ret[i] = (phi * prev if kind == "ar" else 0.0) + eps[i]
    close = 100.0 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[100.0], close[:-1]])
    spread = np.abs(rng.normal(0, 0.5, n)) * vol * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    volume = rng.lognormal(7, 0.4, n)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return _mk_candles(symbol, tf, open_, high, low, close, volume, start)


def load_binance(directory: Path, symbol: str, tf: str):
    rows = []
    for f in sorted(directory.glob(f"{symbol}-{tf}-*")):
        if f.suffix == ".zip":
            with zipfile.ZipFile(f) as zf:
                text = io.TextIOWrapper(zf.open(zf.namelist()[0]))
                rows += [r for r in csv.reader(text) if r and r[0].isdigit()]
        elif f.suffix == ".csv":
            with f.open() as fh:
                rows += [r for r in csv.reader(fh) if r and r[0].isdigit()]
    if not rows:
        raise SystemExit(f"Nenhum arquivo {symbol}-{tf}-* em {directory}")

    def ts(v):
        v = int(v)
        return v / 1e6 if v > 1e14 else v / 1e3

    rows.sort(key=lambda r: int(r[0]))
    seen, out = set(), []
    for r in rows:
        if r[0] in seen:
            raise ValueError(f'Duplicate candle: {r[0]}')
        seen.add(r[0])
        out.append(
            Candle(
                exchange="BINANCE", symbol=symbol, timeframe=tf,
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5]),
                closed_at=datetime.fromtimestamp(ts(r[6]), tz=timezone.utc),
            )
        )
    validate_candles(out, tf)
    return out


def validate_candles(candles, tf):
    if len(candles) < 500:
        raise ValueError(f"Too few candles: {len(candles)}")
    step = timedelta(minutes=TF_MINUTES[tf])
    for i, c in enumerate(candles):
        vals = (c.open, c.high, c.low, c.close, c.volume)
        if any(not math.isfinite(x) for x in vals):
            raise ValueError(f"Non-finite OHLCV at index {i}")
        if min(vals[:4]) <= 0 or c.volume < 0 or c.low > min(c.open,c.close) or c.high < max(c.open,c.close):
            raise ValueError(f"Invalid OHLCV at index {i}")
        if i and (c.closed_at - candles[i - 1].closed_at != step):
            raise ValueError(f"Candle gap at {c.closed_at}")


# ---------------------------------------------------------------- agentes
def build_agents(store: CandleStore):
    ev = SpecialistEvidenceService(None)
    agents = [
        QuantAgent(store),
        TrendAgent(store),
        *build_shadow_specialists(store),
        *build_advanced_shadow_specialists(store),
        *build_month8_shadow_specialists(store, ev),
        *build_month9_shadow_specialists(store),
        *build_month10_shadow_specialists(store),
        *build_month11_shadow_specialists(store),
    ]
    return agents


def to_pos(sig: Signal) -> int:
    return 1 if sig == Signal.BUY else -1 if sig == Signal.SELL else 0


async def collect_positions(candles, stride):
    store = CandleStore(max_candles=max(2000, len(candles) + 10))
    agents = build_agents(store)
    for a in agents:
        await a.initialize()
    names = [a.name for a in agents]
    sym, tf = candles[0].symbol, candles[0].timeframe
    decision_idx, rows, failures = [], [], np.zeros(len(agents), dtype=int)
    for i, c in enumerate(candles):
        store.add(c)
        if i < WARMUP or i + stride + 1 >= len(candles) or (i - WARMUP) % stride:
            continue
        inputs = [
            AgentInput(
                correlation_id=f"{i}", agent_name=a.name, symbol=sym,
                timeframe=tf, market_context={"exchange": "BINANCE"},
            )
            for a in agents
        ]
        outs = await asyncio.gather(
            *(a.run(inp) for a, inp in zip(agents, inputs)), return_exceptions=True
        )
        row = []
        for k, o in enumerate(outs):
            if isinstance(o, Exception) or o.status != AgentStatus.COMPLETED:
                failures[k] += 1
                row.append(0)
            else:
                row.append(to_pos(o.signal))
        decision_idx.append(i)
        rows.append(row)
    return names, np.array(decision_idx), np.array(rows, dtype=float), failures


# ----------------------------------------------------------------- stats
def norm_sf(z):  # P(Z > z)
    return 0.5 * math.erfc(z / math.sqrt(2))


def tstat(x):
    # Newey-West HAC with Bartlett weights; still an approximation.
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 30 or not np.all(np.isfinite(x)):
        return 0.0
    xc = x - x.mean()
    lag = min(36, max(1, int(n ** (1 / 3))))
    lrv = float(np.dot(xc, xc) / n)
    for k in range(1, lag + 1):
        lrv += 2 * (1 - k / (lag + 1)) * float(np.dot(xc[k:], xc[:-k]) / n)
    return 0.0 if lrv <= 0 else float(x.mean() / math.sqrt(lrv / n))


def bh_pass(pvals, q):
    order = np.argsort(pvals)
    m = len(pvals)
    ok = np.zeros(m, dtype=bool)
    thresh = -1
    for rank, idx in enumerate(order, start=1):
        if pvals[idx] <= q * rank / m:
            thresh = rank
    if thresh > 0:
        ok[order[:thresh]] = True
    return ok


def analyse(candles, names, idx, pos, failures, stride, cost, split, fdr):
    tf = candles[0].timeframe
    opens = np.array([c.open for c in candles])
    closes = np.array([c.close for c in candles])
    fwd = opens[idx + stride + 1] / opens[idx + 1] - 1.0
    prev = np.vstack([np.zeros((1, pos.shape[1])), pos[:-1]])
    gross = pos * fwd[:, None]
    cut = int(len(idx) * split)
    prev[cut] = 0.0  # start OOS flat
    net = gross - cost * np.abs(pos - prev)
    net[cut - 1] -= cost * np.abs(pos[cut - 1])
    net[-1] -= cost * np.abs(pos[-1])

    n = len(idx)
    cut = int(n * split)
    units_per_year = 525600 / (TF_MINUTES[tf] * stride)
    active = (pos != 0).sum(axis=0)

    res = []
    for k, name in enumerate(names):
        is_net, oos_net = net[:cut, k], net[cut:, k]
        is_t, oos_t = tstat(is_net), tstat(oos_net)
        res.append(
            {
                "agent": name,
                "active_bars": int(active[k]),
                "failures": int(failures[k]),
                "is_net_mean_bps": float(is_net.mean() * 1e4),
                "is_t": is_t,
                "is_p": norm_sf(is_t),
                "oos_net_mean_bps": float(oos_net.mean() * 1e4),
                "oos_t": oos_t,
                "oos_sharpe": float(
                    oos_net.mean() / oos_net.std(ddof=1) * math.sqrt(units_per_year)
                ) if oos_net.std(ddof=1) > 0 else 0.0,
                "oos_gross_mean_bps": float(gross[cut:, k].mean() * 1e4),
            }
        )

    testable = [i for i, r in enumerate(res) if r["active_bars"] >= 30]
    pvals = np.array([res[i]["is_p"] for i in testable])
    passed = bh_pass(pvals, fdr) if len(testable) else np.array([], dtype=bool)
    surv = [testable[j] for j in range(len(testable)) if passed[j] and res[testable[j]]["is_net_mean_bps"] > 0]
    nominal = [i for i in testable if res[i]["is_p"] < 0.05 and res[i]["is_net_mean_bps"] > 0]
    holds = [i for i in surv if res[i]["oos_net_mean_bps"] > 0 and res[i]["oos_t"] > 1.645]

    # independencia efetiva
    act = [k for k in range(pos.shape[1]) if pos[:, k].std() > 0]
    eff, med_corr = 0.0, 0.0
    if len(act) > 2:
        C = np.corrcoef(pos[:, act].T)
        lam = np.clip(np.linalg.eigvalsh(C), 0, None)
        eff = float(lam.sum() ** 2 / (lam ** 2).sum())
        iu = np.triu_indices(len(act), 1)
        med_corr = float(np.median(np.abs(C[iu])))

    # buy&hold de referencia na janela OOS
    bh_oos = float(fwd[cut:].mean() * 1e4)

    return {
        "n_decisions": int(n), "n_in_sample": int(cut), "n_oos": int(n - cut),
        "agents_total": len(names), "agents_testable": len(testable),
        "agents_never_signal": int(((pos != 0).sum(axis=0) == 0).sum()),
        "agents_constant_signal": int(((pos != 0).sum(axis=0) > 0).astype(int).dot((pos.std(axis=0) == 0).astype(int))),
        "nominal_is_significant": len(nominal),
        "expected_by_chance_nominal": round(0.05 * len(testable), 1),
        "bh_survivors_is": len(surv),
        "survivors_holding_oos": len(holds),
        "survivor_names": [res[i]["agent"] for i in surv],
        "holding_names": [res[i]["agent"] for i in holds],
        "effective_independent_agents": round(eff, 1),
        "median_abs_pairwise_corr": round(med_corr, 3),
        "oos_buy_and_hold_bps_per_decision": round(bh_oos, 2),
        "oos_frac_agents_net_positive": round(
            float(np.mean([r["oos_net_mean_bps"] > 0 for r in res if r["active_bars"] >= 30])), 3
        ) if testable else None,
    }, res


def report(tag, summary, res, top=8):
    print(f"\n===== {tag} =====")
    for k, v in summary.items():
        if k in ("survivor_names", "holding_names"):
            continue
        print(f"{k:38s} {v}")
    print("sobreviventes IS (FDR):", summary["survivor_names"][:15] or "nenhum")
    print("que seguram no OOS   :", summary["holding_names"][:15] or "nenhum")
    t = sorted([r for r in res if r["active_bars"] >= 30], key=lambda r: r["is_t"], reverse=True)[:top]
    print(f"\ntop {top} por t-stat in-sample -> como ficaram fora da amostra")
    print(f"{'agente':44s} {'IS_t':>6s} {'IS_bps':>7s} {'OOS_t':>6s} {'OOS_bps':>8s}")
    for r in t:
        print(f"{r['agent'][:44]:44s} {r['is_t']:6.2f} {r['is_net_mean_bps']:7.2f} {r['oos_t']:6.2f} {r['oos_net_mean_bps']:8.2f}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["synthetic-null", "synthetic-ar", "binance"], required=True)
    ap.add_argument("--dir", type=Path)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--tf", default="1h", choices=list(TF_MINUTES))
    ap.add_argument("--n", type=int, default=3000)
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--cost-bps", type=float, default=12.0, help="custo por lado em bps (taxa+slippage)")
    ap.add_argument("--split", type=float, default=0.6)
    ap.add_argument("--fdr", type=float, default=0.10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--phi", type=float, default=0.12)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()

    if a.mode == "binance":
        if not a.dir:
            sys.exit("--dir obrigatorio")
        candles = load_binance(a.dir, a.symbol, a.tf)
    else:
        candles = synthetic("ar" if a.mode.endswith("ar") else "null", a.n, a.symbol, a.tf, a.seed, a.phi)

    if a.stride < 1 or not (0 < a.split < 1) or not (0 < a.fdr < 1) or a.cost_bps < 0:
        sys.exit("Invalid test parameters")
    if a.mode != "binance":
        validate_candles(candles,a.tf)
    print(f"{len(candles)} candles {a.symbol} {a.tf} | stride={a.stride} custo={a.cost_bps}bps/lado")
    names, idx, pos, fail = await collect_positions(candles, a.stride)
    summary, res = analyse(candles, names, idx, pos, fail, a.stride, a.cost_bps / 1e4, a.split, a.fdr)
    report(f"{a.mode} {a.symbol} {a.tf}", summary, res)
    if a.out:
        a.out.write_text(json.dumps({"meta":{"symbol":a.symbol,"tf":a.tf,"cost_bps":a.cost_bps,"split":a.split,"fdr":a.fdr,"stride":a.stride,"return_model":"open_to_open","stats":"Newey-West HAC"},"summary": summary, "agents": res}, indent=2))
        print("\nsalvo em", a.out)


if __name__ == "__main__":
    asyncio.run(main())