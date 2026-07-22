"""Durable event journal result contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class JournalWriteResult:
    """State returned after idempotently appending an event to the journal."""

    inserted: bool
    broker_published: bool
