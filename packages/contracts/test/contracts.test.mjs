import assert from "node:assert/strict";
import test from "node:test";

import { verifyContracts } from "../scripts/verify-contracts.mjs";

test("all published contracts are versioned and fail closed", async () => {
  assert.equal(await verifyContracts(), 17);
});
