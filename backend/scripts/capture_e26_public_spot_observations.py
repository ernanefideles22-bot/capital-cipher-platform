"""E2.6 explicit one-shot public SPOT candle capture.

Run once every few minutes from a SEPARATE, durable, scheduled research host
starting immediately after the FIRST E2.6 evaluation bar closes. No scheduler
or external durable storage is installed by this file. NEVER routes orders.

The captured observed_at_utc is the local wall clock AFTER Binance response,
not an invented close+1s. Data are stored locally, so independently trusted
clock, storage, append-only attestations and market feed validation are still
needed for defensible prospective evidence. No LIVE/TESTNET credentials used.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.strategy.candidate_replay import BAR_SECONDS
from app.strategy.prospective_core_gate_e26 import (
    CAMPAIGN, MARKETS, HEADER, ALLOWED_SOURCE,
    _datetime,
)

URL = "https://api.binance.com/api/v3/klines"
HTTP_TIMEOUT_SECONDS = 12


def public_spot_klines(symbol: str, interval: str, *, from_ms: int,
                       to_ms: int, limit: int) -> list:
    """Read-only public endpoint, no API keys, no account access."""
    q = urllib.parse.urlencode({
        "symbol":symbol,"interval":interval,
        "startTime":from_ms,"endTime":to_ms,"limit":limit,
    })
    request = urllib.request.Request(
        URL+"?"+q,
        headers={"User-Agent":"CapitalCipher-E26-Research-Only/1.0"},
    )
    with urllib.request.urlopen(request,timeout=HTTP_TIMEOUT_SECONDS) as r:
        rows=json.loads(r.read(2_000_000).decode("utf-8"))
    if not isinstance(rows,list):
        raise ValueError("Binance returned no OHLCV array")
    return rows


def _validated_rows(raw: list, symbol: str, timeframe: str, *,
                    first_close: datetime, last_close: datetime,
                    observed_at: datetime) -> list[dict]:
    step=timedelta(seconds=BAR_SECONDS[timeframe])
    expected=1+int((last_close-first_close)/step)
    if len(raw)!=expected:
        raise ValueError("Incomplete public market candle capture")
    output=[]
    for i,item in enumerate(raw):
        try:
            op=int(item[0])
            opened=datetime.fromtimestamp(op/1000,tz=timezone.utc)
            closed=opened+step
            prices=[float(item[n]) for n in (1,2,3,4,5)]
            exchange_close=int(item[6])
        except (IndexError,ValueError,TypeError,OverflowError) as ex:
            raise ValueError("Malformed source candle") from ex
        if (closed!=first_close+i*step or
            exchange_close!=op+int(step.total_seconds()*1000)-1):
            raise ValueError("Wrong public candle time/continuity")
        o,h,l,c,v=prices
        if (not all(math.isfinite(x) for x in prices) or
            not min(o,h,l,c)>0 or v<0 or
            h<max(o,l,c) or l>min(o,h,c)):
            raise ValueError("Invalid public OHLCV values")
        if closed>observed_at:
            raise ValueError("Unclosed future candle from public provider")
        output.append({
            "open_time_ms":op,"open":str(item[1]),"high":str(item[2]),
            "low":str(item[3]),"close":str(item[4]),
            "volume":str(item[5]),
            "observed_at_utc":observed_at.isoformat(),
            "source":ALLOWED_SOURCE,
        })
    return output


def _append_rows(path: Path, rows: list[dict], *, bootstrap: bool) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    if bootstrap:
        # Exclusive creation: never replace existing future research evidence.
        fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    else:
        if not path.is_file() or path.is_symlink():
            raise ValueError("Missing/symlinked campaign capture file")
        fd=os.open(str(path),os.O_WRONLY|os.O_APPEND)
    try:
        with os.fdopen(fd,"w",encoding="utf-8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=HEADER.split(","),
                                  lineterminator="\n")
            if bootstrap:
                writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        raise


def capture_once(directory: Path, *, fetcher=public_spot_klines,
                 clock=lambda: datetime.now(timezone.utc)) -> dict:
    """One bounded polling cycle; fail closed if any market event was missed."""
    started=_datetime(CAMPAIGN["first_evaluation_candle_closed_at_utc"])
    end=_datetime(CAMPAIGN["evaluation_candle_end_exclusive_utc"])
    now=clock().astimezone(timezone.utc)
    if now < started:
        return {"status":"CAMPAIGN_NOT_STARTED","written":[],
                "collection_is_scheduled":False}
    reports=[]
    for symbol,tf in MARKETS:
        step=timedelta(seconds=BAR_SECONDS[tf])
        path=directory/f"e26_{symbol}_{tf}_observed.csv"
        if not path.exists():
            # Strict first event bootstrap: last 300 previous warmup candles
            # and the first future candle, all fetched in a single response.
            # Running hours/days late must NOT turn history into forward data.
            if now > started+step:
                raise ValueError("Missed first forward candle: cannot backfill")
            first=started-300*step
            last=started
            count=301
            bootstrap=True
        else:
            if path.is_symlink():
                raise ValueError("Symlinked observation file prohibited")
            with path.open(newline="") as stream:
                reader=csv.DictReader(stream)
                if reader.fieldnames!=HEADER.split(","):
                    raise ValueError("Capture header corrupted")
                final=None
                for final in reader:
                    pass
                if final is None:
                    raise ValueError("Empty capture file")
                previous_open=datetime.fromtimestamp(
                    int(final["open_time_ms"])/1000,tz=timezone.utc,
                )
            last=previous_open+2*step
            first=last
            count=1
            bootstrap=False
        if last>=end:
            reports.append({"market":symbol+"/"+tf,"status":"COMPLETE_NO_MORE_CAPTURE"})
            continue
        if now<last:
            reports.append({"market":symbol+"/"+tf,"status":"NEXT_BAR_NOT_CLOSED"})
            continue
        if now>last+step:
            raise ValueError("Missed forward candle, refuse silent backfill")
        start_ms=int((first-step).timestamp()*1000)
        end_ms=int((last-timedelta(milliseconds=1)).timestamp()*1000)
        items=fetcher(symbol,tf,from_ms=start_ms,to_ms=end_ms,limit=count)
        observed=clock().astimezone(timezone.utc)
        if observed>last+step:
            raise ValueError("Public feed arrived too late for forward proof")
        rows=_validated_rows(items,symbol,tf,first_close=first,
                             last_close=last,observed_at=observed)
        _append_rows(path,rows,bootstrap=bootstrap)
        reports.append({
            "market":symbol+"/"+tf,
            "status":"FORWARD_BAR_CAPTURED",
            "bars_written":len(rows),
            "last_closed_at":last.isoformat(),
            "observed_at":observed.isoformat(),
        })
    return {"status":"ONE_READ_ONLY_CAPTURE_CYCLE","written":reports,
            "collection_is_scheduled":False,
            "external_attestation_provided":False,
            "no_order_execution":True}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--directory",type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(capture_once(args.directory),sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
