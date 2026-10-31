# Gate Testnet stop HTTP 400 diagnostic follow-up

Deployment 4fdf8343-4aa0-4ff2-a289-00ac4b88c0a8 ran source
60d48e4ece93bb196519a336fe8533d59b804fbb successfully. The bounded diagnostic
canary 113bb017-e645-5f75-b69b-a796b0c8b85a filled its entry, then the stop
POST failed at STOP_SUBMITTING with HTTP 400 and provider_label=None. The
previous fixed error-code list discarded any unlisted code; the original
response body is unavailable. The precise rejection cause remains unproven.

Signed read-only exchange queries after emergency exit confirmed:

- Entry 82472172996754767: +3 BTC_USDT contracts, filled at 77387.3.
- Exit 82472172996755131: -3 contracts, filled at 77391.9, close and reduce-only
  flags true, left=0, finished/filled, not a liquidation, isolated margin.
- Complete current inventory: zero nonzero positions, open orders and stops.
- Journal: HALTED, stop_id=null, reason "emergency close verified; account
  reconciliation required". Durable risk: active, revision 5.

This change explicitly serializes initial.text=api and pos_margin_mode=isolated
for the stop. Both are documented optional fields; including them removes
implicit source/margin defaults but is a compatibility candidate, not evidence
that either omission caused the rejection. Size=0, close/reduce-only, IOC,
mark-price trigger and adverse trigger direction are preserved.

Diagnostic codes now accept bounded uppercase enum syntax rather than a frozen
list. Credentials, their reflected substrings, signatures, free-form messages
and response bodies are excluded. Only a finite vocabulary of schema field
names is extracted from message/detail, plus response shape. Codes cannot
contain newlines, punctuation, whitespace or more than 64 characters. A failed
write still raises unknown outcome with no automatic retry or fallback payload.

No account recovery, risk reset, budget renewal, new canary or agent activation
is performed by this patch. The spent canary remains spent. The next venue
validation requires reviewed post-fill reconciliation preserving old evidence,
the exact tested source and a new bounded owner grant. A protected canary must
actually confirm an open stop before close, then flat inventory and MATCHED
reconciliation. Emergency exit alone does not qualify or start profitability
sampling.

Official schema: https://www.gate.com/docs/developers/apiv4/en/futures/
