import asyncio, csv, io, json, os, sys, urllib.request, zipfile
from datetime import datetime, timezone
sys.path.insert(0, "backend")
from app.backtesting.engine import BacktestingEngine
from app.schemas.backtest import BacktestRequest
from app.schemas.market import Candle

def month_rows(sym, tf, ym):
    url = f"https://data.binance.vision/data/spot/monthly/klines/{sym}/{tf}/{sym}-{tf}-{ym}.zip"
    try:
        raw = urllib.request.urlopen(url, timeout=60).read()
    except Exception as e:
        print(f"  skip {ym}: {e}", flush=True)
        return []
    zf = zipfile.ZipFile(io.BytesIO(raw))
    rows = list(csv.reader(io.TextIOWrapper(zf.open(zf.namelist()[0]))))
    return [r for r in rows if r and r[0].isdigit()]

def ts(v):
    v = int(v)
    return v / 1e6 if v > 1e14 else v / 1e3

def to_candles(sym, tf, rows):
    return [Candle(exchange="BINANCE", symbol=sym, timeframe=tf,
        open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
        volume=float(r[5]), closed_at=datetime.fromtimestamp(ts(r[6]), tz=timezone.utc)) for r in rows]

MONTHS_15M = ["2026-03","2026-04","2026-05","2026-06"]
MONTHS_1H = ["2025-11","2025-12","2026-01","2026-02","2026-03","2026-04","2026-05","2026-06"]
MONTHS_4H = ["2025-07","2025-08","2025-09","2025-10"] + MONTHS_1H

async def main():
    plans = [("BTCUSDT","15m",MONTHS_15M),("ETHUSDT","15m",MONTHS_15M),("SOLUSDT","15m",MONTHS_15M),("BTCUSDT","1h",MONTHS_1H),("BTCUSDT","4h",MONTHS_4H)]
    results = []
    for sym, tf, months in plans:
        rows = []
        for ym in months:
            rows += month_rows(sym, tf, ym)
        cs = to_candles(sym, tf, rows)
        if not cs:
            print(f"{sym} {tf}: NO DATA", flush=True)
            continue
        print(f"{sym} {tf}: {len(cs)} candles {cs[0].closed_at.date()} -> {cs[-1].closed_at.date()}", flush=True)
        cut = int(len(cs) * 0.7)
        for label, part in (("in_sample", cs[:cut]), ("out_of_sample", cs[cut:]), ("full", cs)):
            r = await BacktestingEngine().run(BacktestRequest(symbol=sym, timeframe=tf), part)
            d = r.model_dump(exclude={"equity_curve"})
            d["window"] = label
            results.append(d)
            print(f"  {label}: trades={r.total_trades} win={r.win_rate}% pf={r.profit_factor} pnl={r.net_pnl_percent}% dd={r.max_drawdown}% blocked={r.blocked_by_risk} decisions={r.decisions}", flush=True)
    os.makedirs("validation", exist_ok=True)
    json.dump(results, open("validation/backtest-results.json", "w"), indent=2, default=str)
    print("saved validation/backtest-results.json")

asyncio.run(main())
