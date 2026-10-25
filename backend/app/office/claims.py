"""Reserve only newly inserted PAPER PRIMARY jobs in their input transaction."""
from __future__ import annotations

from datetime import timedelta
import re

from app.schemas.common import utcnow


def leased_primary_submissions(submissions, worker_id, lease_seconds):
    if worker_id is None:
        return submissions
    if (not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", worker_id)
            or type(lease_seconds) is not int or lease_seconds < 1):
        raise ValueError("Invalid PRIMARY reservation lease")
    if any(job.decision_role != "PRIMARY" or job.execution_mode != "PAPER"
           or job.status != "PENDING" or job.attempt_count != 0
           or job.leased_by is not None or job.lease_expires_at is not None
           for job, _ in submissions):
        raise ValueError("Initial reservation requires new PAPER PRIMARY jobs")
    now = utcnow()
    return [(job.model_copy(update={"status": "LEASED", "attempt_count": 1,
                "leased_by": worker_id, "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now}), memory) for job, memory in submissions]
