"""Baixa candles mensais da Binance (data.binance.vision) para uma pasta.

Uso (no seu PC, que tem acesso):
    python download_klines.py --symbol BTCUSDT --tf 1h --start 2024-01 --end 2026-09 --out klines

Gera arquivos como klines/BTCUSDT-1h-2024-01.zip. Meses indisponiveis sao
ignorados (o mes corrente ainda nao existe como arquivo mensal).
"""

import argparse
import urllib.error
import urllib.request
from pathlib import Path


def months(start: str, end: str):
    y, m = map(int, start.split("-"))
    ye, me = map(int, end.split("-"))
    while (y, m) <= (ye, me):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--start", required=True, help="AAAA-MM")
    ap.add_argument("--end", required=True, help="AAAA-MM")
    ap.add_argument("--out", type=Path, default=Path("klines"))
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    ok = 0
    for ym in months(a.start, a.end):
        name = f"{a.symbol}-{a.tf}-{ym}.zip"
        dest = a.out / name
        if dest.exists():
            ok += 1
            continue
        url = f"https://data.binance.vision/data/spot/monthly/klines/{a.symbol}/{a.tf}/{name}"
        try:
            dest.write_bytes(urllib.request.urlopen(url, timeout=60).read())
            ok += 1
            print("ok  ", name)
        except urllib.error.URLError as e:
            raise RuntimeError(f"Missing or blocked month {name}: {e}") from e
    print(f"{ok} arquivos em {a.out}")


if __name__ == "__main__":
    main()