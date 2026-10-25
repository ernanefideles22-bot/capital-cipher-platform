"""One PostgreSQL statement for newly leased PRIMARY jobs and INPUT evidence."""
from sqlalchemy import column, insert, select, values
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database.models import AgentExecutionJobModel, AgentMemoryEntryModel


def insert_primary_inputs(job_values, input_memories):
    jobs = (pg_insert(AgentExecutionJobModel).values(job_values)
            .on_conflict_do_nothing().returning(AgentExecutionJobModel.execution_id)
            .cte("inserted_primary_jobs"))
    names = ("execution_id", "schema_version", "sequence", "entry_type",
             "payload", "payload_hash", "created_at")
    table = AgentMemoryEntryModel.__table__
    unique = {memory.execution_id: memory for memory in input_memories}
    inputs = values(*(column(name, table.c[name].type) for name in names),
        name="primary_input_values").data([
            tuple(getattr(memory, name) for name in names) for memory in unique.values()
        ])
    # RETURNING is the dependency: do not reread newly inserted jobs from the
    # table in this statement's snapshot. Conflicts receive no second INPUT.
    selected = select(*(inputs.c[name] for name in names)).select_from(
        inputs.join(jobs, inputs.c.execution_id == jobs.c.execution_id))
    return insert(table).from_select(names, selected).returning(table.c.execution_id)
