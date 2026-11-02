"""Opt-in S3 public-observation collector, isolated from the trading runtime.

One bounded queue and one owned SQLite worker. Capture never waits for disk.
Queued != committed: only journal_observations/report describe durable events.
No credentials, network client, broker, OMS or registry are accepted here.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import queue
import shutil
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.schemas.market import Candle
from app.strategy.gate_s3_durable_research import (
    DurableS3ForwardResearch, SCHEMA_VERSION, S3JournalBlocked,
    _deserialize, _serialize,
)
from app.strategy.gate_s3_paired_evidence import read_only_comparison_report
from app.strategy.gate_s3_forward_scorecard import ShadowForwardScorecard

QUEUE_CAPACITY = 12
MAX_COLLECTOR_OBSERVATIONS = 10_000
MIN_FREE_BYTES = 128 * 1024 * 1024
JOURNAL_NAME = "forward-v1.sqlite3"


class GateS3Collector:
    def __init__(self, storage_root, *, mount_verified: bool,
                 journal_factory=DurableS3ForwardResearch):
        self._root = Path(storage_root).absolute()
        self._mount_verified = mount_verified
        self._factory = journal_factory
        self._queue = queue.Queue(maxsize=QUEUE_CAPACITY)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._halt = threading.Event()
        self._accepting = False
        self._thread = None
        self._report = None
        self._state = {
            "status": "WAITING", "error_code": None,
            "journal_observations": 0, "queued_observations": 0,
            "duplicate_candles_skipped": 0, "checked_at": None,
            "max_observations": MAX_COLLECTOR_OBSERVATIONS,
            "execution_authorized": False, "oms_touched": False,
            "profitability_proven": False, "shutdown_pending": False,
            "source_last_observed_at_utc": None, "data_is_current_proven": False,
        }

    def start(self):
        if self._thread is not None:
            raise S3JournalBlocked("S3 collector already started")
        self._accepting = True
        with self._lock:
            self._state["status"] = "STARTING"
        self._thread = threading.Thread(target=self._run, name="gate-s3-research", daemon=True)
        self._thread.start()

    def capture(self, candle: Candle, shadow: dict, *, observed_at: datetime) -> dict:
        """Allowlist and copy into a bounded queue; no synchronous file access."""
        if not self._accepting or self._halt.is_set():
            return {"status": "BLOCKED", "execution_authorized": False}
        try:
            payload = _serialize(candle, shadow, observed_at)
            if len(payload.encode("utf-8")) > 32768:
                raise ValueError("Oversized public observation")
            with self._lock:
                if not self._accepting or self._halt.is_set():
                    return {"status": "BLOCKED", "execution_authorized": False}
                self._queue.put_nowait(payload)
                self._state["queued_observations"] += 1
        except queue.Full:
            self._block("QUEUE_OVERFLOW")
            return {"status": "BLOCKED", "execution_authorized": False}
        except Exception:
            self._block("INVALID_OBSERVATION")
            return {"status": "BLOCKED", "execution_authorized": False}
        return {"status": "QUEUED_NOT_COMMITTED", "execution_authorized": False}

    def snapshot(self):
        with self._lock:
            state = dict(self._state)
            report = copy.deepcopy(self._report)
        state["pending_queue_size"] = self._queue.qsize()
        state["worker_alive"] = bool(self._thread and self._thread.is_alive())
        return {"collection": state, "comparison": report, "execution_authorized": False}

    def stop(self, *, timeout=5.):
        with self._lock:
            self._accepting = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                with self._lock:
                    self._state["shutdown_pending"] = True
                    if not self._halt.is_set():
                        self._state["status"] = "SHUTDOWN_PENDING"

    def _block(self, code):
        self._accepting = False
        self._halt.set()
        with self._lock:
            self._state.update(status="BLOCKED", error_code=code)
            self._report = None
        logging.getLogger("uvicorn.error").warning(
            "GATE_S3_SHADOW_STATUS BLOCKED code=%s execution=false", code)

    def _paths(self):
        root = self._root
        if (self._mount_verified is not True or not root.is_dir()
                or root.is_symlink() or root.resolve() != root):
            raise S3JournalBlocked("S3 requires an explicitly verified persistent root")
        directory = root / "capital-cipher-gate-s3"
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != root:
            raise S3JournalBlocked("Unsafe separate research directory")
        path = directory / JOURNAL_NAME
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                raise S3JournalBlocked("Unsafe research file alias")
        return directory, path

    @staticmethod
    def _shape(candle):
        public = candle.model_dump(mode="json")
        public.pop("received_at")
        return json.dumps(public, sort_keys=True, allow_nan=False)

    def _publish(self, journal, path):
        count = journal.status()["observations"]
        last_clock = max(journal.scorer._last_observed.values(), default=None)
        observed_at = last_clock.isoformat() if last_clock else None
        try:
            report = read_only_comparison_report(path)
        except Exception:
            self._block("REPORT_VERIFICATION_FAILURE")
            with self._lock:
                self._state["journal_observations"] = count
                self._state["source_last_observed_at_utc"] = observed_at
            return
        with self._lock:
            self._state["journal_observations"] = count
            self._state["source_last_observed_at_utc"] = observed_at
            self._state["checked_at"] = datetime.now(timezone.utc).isoformat()
            if not self._halt.is_set():
                self._report = report
                self._state["status"] = "AVAILABLE" if count else "WAITING_FOR_OBSERVATIONS"
        if not self._halt.is_set():
            logging.getLogger("uvicorn.error").info(
                "GATE_S3_SHADOW_STATUS observations=%d execution=false", count)

    def _space_available(self):
        if shutil.disk_usage(self._root).free < MIN_FREE_BYTES:
            self._block("LOW_DISK_SPACE")
            return False
        return True

    def _run(self):
        journal = None
        lock_fd = None
        try:
            import fcntl  # Optional POSIX worker, not required for S3-disabled import.
            directory, path = self._paths()
            if not self._space_available():
                return
            lock_fd = os.open(directory / "writer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if path.exists():
                # Never initialize S3 tables in an unrelated existing database.
                with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as existing:
                    row = existing.execute(
                        "SELECT version FROM s3_schema WHERE name='forward_research'").fetchone()
                    if row != (SCHEMA_VERSION,):
                        raise S3JournalBlocked("Existing file is not an S3 journal")
            journal = self._factory(path)
            last = {}
            for payload, in journal.db.execute("""SELECT o.payload_json FROM s3_observations o
                    JOIN s3_heads h ON h.last_seq=o.seq ORDER BY o.symbol"""):
                candle, shadow, clock = _deserialize(payload)
                last[candle.symbol] = (candle.closed_at, self._shape(candle), clock)
            self._publish(journal, path)
            if journal.status()["observations"] >= MAX_COLLECTOR_OBSERVATIONS:
                self._block("OBSERVATION_LIMIT_REACHED")
            while not self._halt.is_set():
                if self._stop.is_set() and self._queue.empty():
                    break
                try:
                    payload = self._queue.get(timeout=.05)
                except queue.Empty:
                    continue
                try:
                    candle, shadow, clock = _deserialize(payload)
                    ShadowForwardScorecard._validate(candle, shadow, clock)
                    prior = last.get(candle.symbol)
                    shape = self._shape(candle)
                    if prior and candle.closed_at == prior[0]:
                        if shape != prior[1] or clock < prior[2]:
                            raise S3JournalBlocked("Changed candle or backwards duplicate clock")
                        # First actual observation wins. Never replace its decision,
                        # receipt clock or fill timing with a later API snapshot.
                        with self._lock:
                            self._state["duplicate_candles_skipped"] += 1
                        continue
                    if journal.status()["observations"] >= MAX_COLLECTOR_OBSERVATIONS:
                        self._block("OBSERVATION_LIMIT_REACHED")
                        break
                    if self._halt.is_set():
                        break
                    if not self._space_available():
                        break
                    journal.record(candle, shadow, observed_at=clock)
                    last[candle.symbol] = (candle.closed_at, shape, clock)
                    self._publish(journal, path)
                finally:
                    self._queue.task_done()
        except Exception:
            if not self._halt.is_set():
                self._block("STORAGE_OR_REPLAY_FAILURE")
        finally:
            if journal is not None:
                try:
                    journal.close()
                except Exception:
                    self._block("STORAGE_CLOSE_FAILURE")
            if lock_fd is not None:
                os.close(lock_fd)
            with self._lock:
                self._state["shutdown_pending"] = False
                if not self._halt.is_set():
                    self._state["status"] = "STOPPED"
            self._accepting = False
