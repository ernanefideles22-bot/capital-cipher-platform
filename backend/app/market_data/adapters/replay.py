"""Replay adapter: re-emit candles already stored in memory/DB (docs/33)."""

from __future__ import annotations

from app.market_data.adapters.base import MarketDataAdapter
from app.market_data.identity import candle_dataset_hash, candle_event_id
from app.replay.checkpoints import CheckpointStore
from app.schemas.common import utcnow
from app.schemas.market import Candle
from app.schemas.replay import ReplayCheckpoint, ReplayResult

REPLAY_TOPIC = "market.replay.v1"


class ReplayDatasetChangedError(ValueError):
    pass


class ReplayCheckpointCorruptError(ValueError):
    pass


class ReplayMarketDataAdapter(MarketDataAdapter):
    exchange_name = "REPLAY"

    def __init__(
        self,
        candles: list[Candle],
        *,
        replay_id: str | None = None,
        consumer_name: str = "market-replay",
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        super().__init__()
        self._candles = sorted(
            candles,
            key=lambda candle: (
                candle.closed_at,
                candle.exchange.value,
                candle.symbol,
                candle.timeframe,
            ),
        )
        self.dataset_hash = candle_dataset_hash(self._candles)
        self.replay_id = replay_id or f"market:{self.dataset_hash[:24]}"
        self.consumer_name = consumer_name
        self._checkpoint_store = checkpoint_store

    async def connect(self) -> None:
        self.connected = True
        await self._emit_status("MARKET_CONNECTED", {"source": "replay"})

    async def disconnect(self) -> None:
        self.connected = False
        await self._emit_status("MARKET_DISCONNECTED", {"source": "replay"})

    async def subscribe_candles(self, symbol: str, timeframe: str) -> None:
        """No-op for replay."""

    async def replay(self, *, reset: bool = False) -> ReplayResult:
        checkpoint = None
        if self._checkpoint_store is not None and not reset:
            checkpoint = await self._checkpoint_store.load_replay_checkpoint(
                self.replay_id,
                self.consumer_name,
                REPLAY_TOPIC,
            )

        resumed = checkpoint is not None
        if checkpoint is not None and checkpoint.dataset_hash != self.dataset_hash:
            raise ReplayDatasetChangedError(
                "Replay dataset differs from the checkpointed dataset"
            )

        start_offset = checkpoint.next_offset if checkpoint is not None else 0
        if start_offset > len(self._candles):
            raise ReplayCheckpointCorruptError(
                "Replay checkpoint points beyond the dataset"
            )
        if start_offset > 0 and checkpoint is not None:
            expected_last_id = candle_event_id(self._candles[start_offset - 1])
            if checkpoint.last_event_id != expected_last_id:
                raise ReplayCheckpointCorruptError(
                    "Replay checkpoint last_event_id does not match the dataset"
                )

        if checkpoint is not None and checkpoint.status == "COMPLETED":
            return ReplayResult(
                replay_id=self.replay_id,
                dataset_hash=self.dataset_hash,
                start_offset=start_offset,
                next_offset=start_offset,
                events_emitted=0,
                total_events=len(self._candles),
                resumed=True,
                completed=True,
            )

        current = ReplayCheckpoint(
            replay_id=self.replay_id,
            consumer_name=self.consumer_name,
            topic=REPLAY_TOPIC,
            dataset_hash=self.dataset_hash,
            next_offset=start_offset,
            last_event_id=checkpoint.last_event_id if checkpoint is not None else None,
            events_processed=start_offset,
            status="COMPLETED" if not self._candles else "RUNNING",
            completed_at=utcnow() if not self._candles else None,
        )
        if self._checkpoint_store is not None:
            await self._checkpoint_store.save_replay_checkpoint(current)

        emitted = 0
        try:
            for index in range(start_offset, len(self._candles)):
                candle = self._candles[index]
                await self._emit_candle(candle)
                emitted += 1
                next_offset = index + 1
                completed = next_offset == len(self._candles)
                current = current.model_copy(
                    update={
                        "next_offset": next_offset,
                        "last_event_id": candle_event_id(candle),
                        "events_processed": next_offset,
                        "status": "COMPLETED" if completed else "RUNNING",
                        "updated_at": utcnow(),
                        "completed_at": utcnow() if completed else None,
                    }
                )
                if self._checkpoint_store is not None:
                    await self._checkpoint_store.save_replay_checkpoint(current)
        except Exception:
            if self._checkpoint_store is not None:
                failed = current.model_copy(
                    update={
                        "status": "FAILED",
                        "updated_at": utcnow(),
                        "completed_at": None,
                    }
                )
                await self._checkpoint_store.save_replay_checkpoint(failed)
            raise

        return ReplayResult(
            replay_id=self.replay_id,
            dataset_hash=self.dataset_hash,
            start_offset=start_offset,
            next_offset=current.next_offset,
            events_emitted=emitted,
            total_events=len(self._candles),
            resumed=resumed,
            completed=current.status == "COMPLETED",
        )
