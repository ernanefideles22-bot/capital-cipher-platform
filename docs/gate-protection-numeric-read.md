# Gate durable protection: NUMERIC and float read boundary

The first explicitly granted technical canary on source
6b145a3017c7f787824234f9d0aa0ef6b81bc3af queued OMS order
927b9bc1-14eb-5104-ac40-a58545edee2a. Its command was consumed once and
stopped at protection verification, before the journal cycle / entry intent and
before any exchange POST. The OMS conservatively recorded UNKNOWN and raised
the durable kill switch (risk-control revision 1). This is not real trade proof.

Read-only inspection of the existing staging database confirmed:
- SELECT privileges and the immutable protection row exist.
- The persisted reference is NUMERIC 77165.429999999993015081.
- OMSOrder projects it to float 77165.43.
- Stop 76666.900000000000000000, target 78162.490000000000000000,
  leverage 1 and all identity fields are present.
- The pure validator compared these different representations exactly and failed.
- No Gate journal cycle or entry identity was created for this command.

The store now first joins the protection to its exact OMS identity and compares
the original NUMERIC reference and leverage inside PostgreSQL. Only an exact
match is projected to DOUBLE PRECISION, matching the existing OMS float model.
The strict pure validator is unchanged; no price-distance, tick or approximate
matching is introduced. Two distinct stored NUMERIC values that map to the same
float still fail the join. A different model float still fails validation.

The corrected SELECT and unchanged validator were exercised against the actual
failed staging record, in a read-only transaction: verified=true,
execution_authorized=false. No order, stop, close or risk-control change was
performed by that diagnostic. This establishes the read correction only.

Regression coverage includes the observed decimal representation, a distinct
stored value within the same float rounding interval, a different model price,
and actual disposable PG17 risk -> repository -> immutable trigger -> store
reads at integer and non-integer reference prices. Production triggers remain
immutable; corruption injection runs only in a newly created local test DB.

Operational recovery must preserve the consumed command, old grant reservation,
risk trigger, OMS transitions and all historical evidence. Never requeue the old
command or claim it filled/canceled at Gate. The failed pre-write attempt needs
an explicit audited maintenance disposition, fresh signed empty account evidence,
and a reviewed risk-control transition before another bounded canary can run.
The numeric read fix itself does not reset a halt, create a grant or send orders.
