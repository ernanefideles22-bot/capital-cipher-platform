"""One-shot, pre-registered forward-holdout research evaluation.

This is an OFFLINE prototype, NOT a secure holdout service. True sealing
requires a separately administered dataset, signing key, and append-only
audit outside agent/repository control. It cannot authorize ANY execution.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Callable, Sequence

from app.schemas.common import Exchange
from app.schemas.market import Candle
from app.strategy.candidate_replay import (
    BAR_SECONDS, _validate_bars, replay_candidate,
)
from app.strategy.research_lab import StrategyHypothesis


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware timestamp is required")
    return value.astimezone(timezone.utc)


def canonical(data: object) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def sha256(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def digest_valid(value: str, digits: int) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{%d}" % digits, value))


@dataclass(frozen=True)
class ForwardCampaign:
    campaign_id: str
    frozen_commit_sha: str
    training_dataset_sha256: str
    training_ends_at: datetime
    holdout_starts_at: datetime
    holdout_ends_at: datetime
    source_exchange: str
    symbol: str
    timeframe: str
    candidates: tuple[StrategyHypothesis, ...]
    candidate_budget: int
    embargo_candles: int = 2

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}", self.campaign_id):
            raise ValueError("Invalid campaign ID")
        if not digest_valid(self.frozen_commit_sha, 40):
            raise ValueError("Full immutable Git commit SHA required")
        if not digest_valid(self.training_dataset_sha256, 64):
            raise ValueError("Training data hash required")
        if self.source_exchange != Exchange.GATE.value:
            raise ValueError("This forward campaign requires Gate market data")
        if self.timeframe not in BAR_SECONDS:
            raise ValueError("Unsupported research timeframe")
        if not 1 <= self.candidate_budget <= 32:
            raise ValueError("Candidate search budget must be 1..32")
        if not 1 <= len(self.candidates) <= self.candidate_budget:
            raise ValueError("Frozen candidates exceed preregistered budget")
        if not 1 <= self.embargo_candles <= 48:
            raise ValueError("Embargo must be 1..48 candles")
        if len({c.fingerprint for c in self.candidates}) != len(self.candidates):
            raise ValueError("Repeated frozen hypothesis")
        for candidate in self.candidates:
            if (candidate.symbol != self.symbol or
                candidate.timeframe != self.timeframe):
                raise ValueError("Mixed candidate universe")
        train, start, end = (
            utc(self.training_ends_at), utc(self.holdout_starts_at),
            utc(self.holdout_ends_at),
        )
        step = BAR_SECONDS[self.timeframe]
        if start < train + timedelta(seconds=step * self.embargo_candles):
            raise ValueError("Holdout must follow training and embargo")
        duration = (end - start).total_seconds()
        if duration < 60 * step or duration % step != 0:
            raise ValueError("Holdout must be >=60 exact complete candles")

    def payload(self) -> dict:
        raw = asdict(self)
        for name in ("training_ends_at", "holdout_starts_at", "holdout_ends_at"):
            raw[name] = utc(getattr(self, name)).isoformat()
        raw["candidate_fingerprints"] = [c.fingerprint for c in self.candidates]
        return raw

    @property
    def fingerprint(self) -> str:
        return sha256(self.payload())


@dataclass(frozen=True)
class CandidateHoldoutResult:
    hypothesis_fingerprint: str
    trades: int
    net_usdt_12bps: float
    net_usdt_25bps: float
    maximum_drawdown_percent: float
    oos_status: str = "EXPLORATORY_FOR_HUMAN_REVIEW_ONLY"
    execution_authorized: bool = False


@dataclass(frozen=True)
class HoldoutReceipt:
    campaign_id: str
    campaign_sha256: str
    data_sha256: str
    consumed_at: str
    candidates: tuple[CandidateHoldoutResult, ...]
    receipt_hmac_sha256: str
    status: str = "OFFLINE_RESEARCH_ONLY"
    execution_authorized: bool = False


class ForwardHoldoutRegistry:
    """Immutable plan + one-shot consumption + result receipt.

    SQLite on a machine controlled by the researcher is insufficient for
    independent attestation. Production requires a TRUSTED runner that alone
    owns both the signing secret and a durable external consumption ledger.
    """

    def __init__(
        self, path: str | Path, *, signing_key: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("Independent evaluator requires >=256-bit HMAC secret")
        self._key = signing_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.path = Path(path)
        if str(self.path) == ":memory:" or self.path.is_symlink():
            raise ValueError("Durable, non-symlink research registry required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS frozen_campaigns(
                campaign_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                campaign_sha256 TEXT NOT NULL,
                signed_hmac TEXT NOT NULL,
                frozen_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS consumption(
                campaign_id TEXT PRIMARY KEY,
                consumed_at TEXT NOT NULL,
                dataset_claim_sha256 TEXT NOT NULL,
                FOREIGN KEY(campaign_id) REFERENCES frozen_campaigns(campaign_id)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS receipts(
                campaign_id TEXT PRIMARY KEY,
                receipt_json TEXT NOT NULL,
                result_hmac TEXT NOT NULL,
                FOREIGN KEY(campaign_id) REFERENCES frozen_campaigns(campaign_id)
            )""")
            for name in ("frozen_campaigns", "consumption", "receipts"):
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS {name}_immutable_update
                    BEFORE UPDATE ON {name} BEGIN
                        SELECT RAISE(ABORT, 'holdout audit records immutable');
                    END""")
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS {name}_immutable_delete
                    BEFORE DELETE ON {name} BEGIN
                        SELECT RAISE(ABORT, 'holdout audit records immutable');
                    END""")

    @contextmanager
    def _db(self):
        with closing(sqlite3.connect(str(self.path), timeout=10)) as db:
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db

    def _sign(self, blob: str) -> str:
        return hmac.new(self._key, blob.encode("utf-8"), hashlib.sha256).hexdigest()

    def register(self, campaign: ForwardCampaign) -> str:
        """Freeze all candidates BEFORE the forward period begins."""
        now = utc(self._clock())
        if now >= utc(campaign.holdout_starts_at):
            raise ValueError("Campaign must be frozen before future holdout begins")
        blob = canonical(campaign.payload())
        fingerprint = hashlib.sha256(blob.encode()).hexdigest()
        signature = self._sign(blob)
        with self._db() as db:
            db.execute("INSERT INTO frozen_campaigns VALUES (?,?,?,?,?)", (
                campaign.campaign_id, blob, fingerprint, signature,
                now.isoformat(),
            ))
        return fingerprint

    def _get_verified(self, db, campaign: ForwardCampaign) -> str:
        row = db.execute(
            "SELECT payload_json,campaign_sha256,signed_hmac,frozen_at "
            "FROM frozen_campaigns WHERE campaign_id=?", (campaign.campaign_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Campaign was not preregistered")
        blob, fingerprint, signature, frozen_at = row
        if (fingerprint != campaign.fingerprint
            or hashlib.sha256(blob.encode()).hexdigest() != fingerprint
            or not hmac.compare_digest(self._sign(blob), signature)
            or not hmac.compare_digest(canonical(campaign.payload()), blob)):
            raise ValueError("Frozen strategy identity/integrity mismatch")
        if utc(datetime.fromisoformat(frozen_at)) >= utc(campaign.holdout_starts_at):
            raise ValueError("Campaign was registered after the holdout start")
        return fingerprint

    def consumed(self, campaign_id: str) -> bool:
        with self._db() as db:
            return db.execute(
                "SELECT 1 FROM consumption WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone() is not None

    def evaluate_once(
        self, campaign: ForwardCampaign, candles: Sequence[Candle],
    ) -> HoldoutReceipt:
        """Reserve one-time read BEFORE validating the future outcome.

        A failed evaluation still consumes the campaign; do not permit
        repeated peeking/tuning on a supposedly sealed evaluation period.
        """
        now = utc(self._clock())
        if now < utc(campaign.holdout_ends_at) + timedelta(
            seconds=BAR_SECONDS[campaign.timeframe]
        ):
            raise ValueError("Forward holdout not fully complete")
        # Claim happens BEFORE dataset content is inspected. The claimed
        # data SHA is an opaque exact input fingerprint, not source provenance.
        claim_sha = sha256([b.model_dump(mode="json") for b in candles])
        with self._db() as db:
            self._get_verified(db, campaign)
            db.execute("INSERT INTO consumption VALUES (?,?,?)", (
                campaign.campaign_id, now.isoformat(), claim_sha,
            ))

        # Any failure beyond this point is terminal for this campaign ID.
        step = timedelta(seconds=BAR_SECONDS[campaign.timeframe])
        start, end = (
            utc(campaign.holdout_starts_at),
            utc(campaign.holdout_ends_at),
        )
        expected_count = 60 + int((end - start) / step)
        if len(candles) != expected_count:
            raise ValueError("Holdout requires exact 60 warmup + evaluation bars")
        if any(b.exchange != Exchange.GATE for b in candles):
            raise ValueError("Holdout must use Gate-identified closed candles")
        if any(utc(b.received_at) > now for b in candles):
            raise ValueError("Holdout contains future/unavailable observations")
        if (utc(candles[59].closed_at) >= start
            or utc(candles[60].closed_at) < start
            or utc(candles[-1].closed_at) >= end):
            raise ValueError("Holdout contains warmup/evaluation boundary error")
        # The last validation checks chronological identity and gaps.
        _validate_bars(campaign.candidates[0], candles)
        results = []
        for candidate in campaign.candidates:
            base = replay_candidate(
                candidate, candles, evaluation_start=60,
                all_in_cost_bps_per_side=12,
            )
            stress = replay_candidate(
                candidate, candles, evaluation_start=60,
                all_in_cost_bps_per_side=25,
            )
            if base.dataset_hash != stress.dataset_hash:
                raise ValueError("Inconsistent execution-stress dataset")
            results.append(CandidateHoldoutResult(
                hypothesis_fingerprint=candidate.fingerprint,
                trades=len(base.trades),
                net_usdt_12bps=base.net_pnl,
                net_usdt_25bps=stress.net_pnl,
                maximum_drawdown_percent=base.max_drawdown_percent,
            ))
        with self._db() as db:
            claim = db.execute(
                "SELECT consumed_at,dataset_claim_sha256 FROM consumption "
                "WHERE campaign_id=?", (campaign.campaign_id,)
            ).fetchone()
            if not claim or claim[1] != claim_sha:
                raise ValueError("One-shot consumption record mismatch")
            payload = {
                "campaign_id": campaign.campaign_id,
                "campaign_sha256": campaign.fingerprint,
                "data_sha256": claim_sha,
                "consumed_at": claim[0],
                "candidates": [asdict(r) for r in results],
                "status": "OFFLINE_RESEARCH_ONLY",
                "execution_authorized": False,
            }
            body = canonical(payload)
            tag = self._sign(body)
            db.execute("INSERT INTO receipts VALUES (?,?,?)",
                       (campaign.campaign_id, body, tag))
        return HoldoutReceipt(
            campaign.campaign_id, campaign.fingerprint, claim_sha, claim[0],
            tuple(results), tag,
        )

    def verify_receipt(self, campaign: ForwardCampaign) -> dict:
        """Authenticate local result bytes; requires trusted external key custody."""
        with self._db() as db:
            self._get_verified(db, campaign)
            row = db.execute(
                "SELECT receipt_json,result_hmac FROM receipts WHERE campaign_id=?",
                (campaign.campaign_id,),
            ).fetchone()
            if row is None or not hmac.compare_digest(self._sign(row[0]), row[1]):
                raise ValueError("Missing/invalid signed holdout receipt")
            payload = json.loads(row[0])
            if (payload["campaign_sha256"] != campaign.fingerprint
                or payload.get("execution_authorized") is not False):
                raise ValueError("Untrusted holdout receipt")
            return payload
