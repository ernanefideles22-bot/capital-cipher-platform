"""Content-addressed, offline historical Gate quote/funding tape.

Accepts independently collected data and records provenance. A SHA256 manifest
detects byte/record drift, NOT whether venue observations are authentic.
No network requests, private keys, OMS or broker execution.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from app.strategy.venue_research_execution import (
    ContractSpec, FundingEvent, HistoricalQuoteTape, Quote, utc,
)


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class TapeManifest:
    contract: str
    source: str
    source_archive_id: str
    collected_at: str
    quote_count: int
    funding_count: int
    first_quote_received_at: str
    last_quote_received_at: str
    first_funding_settled_at: str | None
    last_funding_settled_at: str | None
    records_sha256: str
    dataset_sha256: str


@dataclass(frozen=True)
class HistoricalTapeDataset:
    spec: ContractSpec
    quotes: tuple[Quote, ...]
    funding: tuple[FundingEvent, ...]
    source: str
    source_archive_id: str
    collected_at: datetime
    manifest: TapeManifest

    def tape(
        self, *, max_age_seconds: int = 60,
        extra_slippage_bps: float = 12.0,
    ) -> HistoricalQuoteTape:
        return HistoricalQuoteTape(
            self.spec, self.quotes, max_age_seconds=max_age_seconds,
            extra_slippage_bps=extra_slippage_bps,
        )


def _records(spec: ContractSpec, quotes: Sequence[Quote],
             funding: Sequence[FundingEvent]) -> dict:
    return {
        "spec": asdict(spec),
        "quotes": [
            {**asdict(item), "observed_at": utc(item.observed_at).isoformat(),
             "received_at": utc(item.received_at).isoformat()}
            for item in quotes
        ],
        "funding": [
            {**asdict(item), "settled_at": utc(item.settled_at).isoformat(),
             "received_at": utc(item.received_at).isoformat()}
            for item in funding
        ],
    }


def build_tape_dataset(
    spec: ContractSpec,
    quotes: Sequence[Quote],
    funding: Sequence[FundingEvent] = (),
    *, source: str, source_archive_id: str,
    collected_at: datetime,
) -> HistoricalTapeDataset:
    if not (3 <= len(source) <= 128 and 8 <= len(source_archive_id) <= 256):
        raise ValueError("Independently traceable source and archive ID required")
    if len(quotes) < 2:
        raise ValueError("At least two quotes required")
    captured = utc(collected_at)
    # This validates strict receipt order and exchange/contract.
    HistoricalQuoteTape(spec, quotes)
    prior = None
    seen_funding: set[str] = set()
    for event in funding:
        if event.contract != spec.symbol:
            raise ValueError("Mixed funding contract")
        settled, received = utc(event.settled_at), utc(event.received_at)
        if settled > captured or received > captured:
            raise ValueError("Future funding in captured dataset")
        if prior is not None and settled <= prior:
            raise ValueError("Funding events must be strictly ordered by settlement")
        if event.funding_id in seen_funding:
            raise ValueError("Duplicate funding source identity")
        prior = settled
        seen_funding.add(event.funding_id)
    for item in quotes:
        if utc(item.observed_at) > captured or utc(item.received_at) > captured:
            raise ValueError("Future quote in captured dataset")
    rows = _records(spec, quotes, funding)
    rows_sha = hashlib.sha256(canonical_json(rows).encode()).hexdigest()
    context = {
        "schema": "venue-tape-d22-v1",
        "source": source,
        "archive_id": source_archive_id,
        "collected_at": captured.isoformat(),
        "records_sha256": rows_sha,
    }
    dataset_sha = hashlib.sha256(canonical_json(context).encode()).hexdigest()
    fund = list(funding)
    manifest = TapeManifest(
        contract=spec.symbol, source=source,
        source_archive_id=source_archive_id,
        collected_at=captured.isoformat(),
        quote_count=len(quotes), funding_count=len(fund),
        first_quote_received_at=utc(quotes[0].received_at).isoformat(),
        last_quote_received_at=utc(quotes[-1].received_at).isoformat(),
        first_funding_settled_at=(utc(fund[0].settled_at).isoformat()
                                  if fund else None),
        last_funding_settled_at=(utc(fund[-1].settled_at).isoformat()
                                 if fund else None),
        records_sha256=rows_sha,
        dataset_sha256=dataset_sha,
    )
    return HistoricalTapeDataset(
        spec, tuple(quotes), tuple(fund), source, source_archive_id,
        captured, manifest,
    )


def save_tape_dataset(dataset: HistoricalTapeDataset, path: str | Path) -> None:
    """Export for archival review, never silently overwrite prior evidence."""
    payload = {
        "manifest": asdict(dataset.manifest),
        "records": _records(dataset.spec, dataset.quotes, dataset.funding),
    }
    with open(path, "x", encoding="utf-8") as file:
        file.write(canonical_json(payload) + "\n")


def load_tape_dataset(path: str | Path) -> HistoricalTapeDataset:
    """Read only a declared archive; recalculate and compare both hashes."""
    raw = Path(path).read_bytes()
    if len(raw) > 100_000_000:
        raise ValueError("Historical tape exceeds 100 MB import limit")
    payload = json.loads(raw.decode("utf-8"))
    if set(payload) != {"manifest", "records"}:
        raise ValueError("Unknown tape document shape")
    manifest = TapeManifest(**payload["manifest"])
    rows = payload["records"]
    spec = ContractSpec(**rows["spec"])
    quotes = [
        Quote(**{**item, "observed_at": datetime.fromisoformat(item["observed_at"]),
                 "received_at": datetime.fromisoformat(item["received_at"])})
        for item in rows["quotes"]
    ]
    funding = [
        FundingEvent(**{**item, "settled_at": datetime.fromisoformat(item["settled_at"]),
                        "received_at": datetime.fromisoformat(item["received_at"])})
        for item in rows["funding"]
    ]
    result = build_tape_dataset(
        spec, quotes, funding,
        source=manifest.source, source_archive_id=manifest.source_archive_id,
        collected_at=datetime.fromisoformat(manifest.collected_at),
    )
    if result.manifest != manifest:
        raise ValueError("Historical tape manifest mismatch; archive changed")
    return result
