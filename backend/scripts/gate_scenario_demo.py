"""Synthetic engineering demonstration, never market evidence or simulated PnL."""
import argparse
from datetime import datetime, timedelta, timezone
from math import sin, pi
from pathlib import Path

from app.schemas.market import Candle
from app.strategy import gate_scenario_journal as journal
from app.strategy import gate_scenario_observer as observer


START = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def candle(tf, when, price, *, high=None, low=None, opened=None):
    return Candle(exchange="GATE", symbol="BTCUSDT", timeframe=tf,
        open=price if opened is None else opened, high=price+1 if high is None else high,
        low=price-1 if low is None else low, close=price, volume=100,
        closed_at=when, received_at=when+timedelta(seconds=1)).model_dump(mode="json")


def synthetic_snapshots(case="confirmed", *, side="SHORT"):
    """Every intervening minute is provided; all prices are invented near 100."""
    if case not in {"confirmed", "failed_recheck", "invalidated"} or side not in {"SHORT", "LONG"}:
        raise ValueError("Unknown synthetic demonstration")
    frames = {}
    for tf, period in (("4h",240),("1h",60)):
        prices = [130-.25*i+1.5*sin(i*pi/4) for i in range(100)]
        offset = 101-prices[-1]
        frames[tf] = [candle(tf,START-timedelta(minutes=period*(99-i)),p+offset)
                      for i,p in enumerate(prices)]
    prices = [70+2*sin(i*pi/4) for i in range(80)] + [80,90,97,102,105,103,100,99,100,101]
    frames["15m"] = [candle("15m",START-timedelta(minutes=15*(len(prices)-1-i)),p)
                     for i,p in enumerate(prices)]
    for minute_index in range(47):
        now = START+timedelta(minutes=minute_index)
        if minute_index == 15:
            frames["15m"].append(candle("15m",now,105.5,high=106.5,low=103,opened=104))
        if minute_index == 30:
            frames["15m"].append(candle("15m",now,103,high=106,low=102,opened=105.5))
        if minute_index == 45:
            frames["15m"].append(candle("15m",now,97.8,high=104,low=97.5,opened=103))
        price = 101 if minute_index < 15 else 105.5 if minute_index < 30 else 103 if minute_index < 45 else 97.8
        if case == "invalidated" and minute_index >= 20:
            price = 108
        if minute_index == 46:
            price = 99.5 if case == "failed_recheck" else 97.6 if case == "confirmed" else 108
        payload = {"as_of": (now+timedelta(seconds=2)).isoformat(),
            "candles": {tf:list(values) for tf,values in frames.items()},
            "minute": candle("1m",now,price,high=price+.1,low=price-.1),
            "source_label": "SYNTHETIC_ENGINEERING_FIXTURE_NOT_MARKET_DATA"}
        if side == "LONG":
            for values in payload["candles"].values():
                for i, value in enumerate(values):
                    values[i] = {**value, "open":200-value["open"], "close":200-value["close"],
                                 "high":200-value["low"], "low":200-value["high"]}
            payload["minute"] = {**payload["minute"], "open":200-payload["minute"]["open"],
                "close":200-payload["minute"]["close"], "high":200-payload["minute"]["low"],
                "low":200-payload["minute"]["high"]}
        yield payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    result = {"synthetic_only":True,"financial_replays":0,"cases":[]}
    for side in ("SHORT","LONG"):
        for case in ("confirmed","failed_recheck","invalidated"):
            folder = args.root / f"{side.lower()}-{case}"
            for index, snapshot in enumerate(synthetic_snapshots(case,side=side)):
                report = journal.register(folder,snapshot) if index == 0 else journal.observe(folder,snapshot)
                if report["status"] in observer.TERMINAL | {"BLOCKED"}:
                    break
            result["cases"].append({"side":side,"case":case,"result":report})
    (args.root/"synthetic-demo.json").write_text(observer.canonical(result)+"\n")
    print(observer.canonical({"synthetic_only":True,"cases":[
        {"side":v["side"],"case":v["case"],"status":v["result"]["status"],
         "new_candidates":v["result"]["new_candidate_count"]} for v in result["cases"]]}))


if __name__ == "__main__":
    main()
