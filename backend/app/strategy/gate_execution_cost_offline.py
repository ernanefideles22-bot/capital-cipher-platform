"""Post-capture quote supplement for incomplete public size metadata.

No new GETs, no change to the original audit or its journal. Missing limits
are explicitly unknown; zero contract minimum does not certify execution.
"""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.strategy import gate_execution_cost_audit as original


def partial_metadata(spot, perp, symbol):
    if (spot.get("id") != symbol or spot.get("trade_status") != "tradable" or
            perp.get("name") != symbol or perp.get("type") != "direct" or
            perp.get("in_delisting") is not False):
        raise ValueError("Unsupported/nontradable market")
    precision = spot["amount_precision"]
    if type(precision) is not int or not 0 <= precision <= 18:
        raise ValueError("Invalid spot precision")
    mult = original.D(perp["quanto_multiplier"])
    minimum, maximum = original.D(perp["order_size_min"]), original.D(perp["order_size_max"])
    if mult <= 0 or not 0 <= minimum <= maximum or maximum <= 0:
        raise ValueError("Invalid public contract bounds")
    unknown = []
    normalized_spot = copy.deepcopy(spot)
    for key in ("min_base_amount", "min_quote_amount", "max_base_amount", "max_quote_amount"):
        if key not in spot:
            unknown.append("spot." + key)
            # Internal quote arithmetic only. Never present this placeholder as
            # an explicit exchange null/no-limit field in source evidence.
            normalized_spot[key] = None
        elif spot[key] is not None and original.D(spot[key]) < 0:
            raise ValueError("Invalid spot limit")
    if minimum == 0:
        unknown.append("perp.effective_minimum_positive_size")
    return {"spot": normalized_spot, "perp": copy.deepcopy(perp), "multiplier": str(mult),
            "spot_quantum": str(original.Decimal(1).scaleb(-precision)),
            "public_perp_taker_fee_reference": perp.get("taker_fee_rate"),
            "personal_fee_verified": False, "unverified_size_limits": unknown,
            "source_contract_minimum": str(minimum), "whole_positive_contracts_only": True}


def without_empty_levels(receipt, venue):
    """Zero available size contributes no liquidity; keep negative/malformed rows.

    Original response bytes/hashes are never changed. The ephemeral arithmetic
    input is labelled derived and does not retain a contradictory raw hash.
    """
    try:
        raw = json.loads(receipt["raw_response_utf8"])
        cleaned, ignored = copy.deepcopy(raw), 0
        for side in ("asks", "bids"):
            kept = []
            for level in raw[side]:
                try:
                    size = original.D(level[1] if venue == "spot" else level["s"])
                    price = original.D(level[0] if venue == "spot" else level["p"])
                except (ValueError, KeyError, IndexError, TypeError):
                    kept.append(level)
                    continue
                if size == 0 and price > 0:
                    ignored += 1
                else:
                    kept.append(level)
            cleaned[side] = kept
        value = dict(receipt)
        value["derived_from_response_sha256"] = value.pop("response_sha256", None)
        value["raw_response_utf8"] = original.base.canonical(cleaned)
        value["derived_arithmetic_input_not_original_response"] = True
        return value, ignored
    except (ValueError, KeyError, TypeError):
        return receipt, 0


def analyze(root):
    root = Path(root)
    prior = original.review(root)
    if not prior["complete"]:
        raise ValueError("Complete preserved original audit required")
    before = hashlib.sha256(original.base._path(root).read_bytes()).hexdigest()
    sources, hashes = {}, {}
    for p in sorted((root / "raw").glob("*.json")):
        raw = p.read_bytes()
        hashes[p.name] = hashlib.sha256(raw).hexdigest()
        receipt = json.loads(raw)
        if "raw_response_utf8" in receipt:
            if hashlib.sha256(receipt["raw_response_utf8"].encode()).hexdigest() != receipt["response_sha256"]:
                raise ValueError("Archived public response hash mismatch")
        sources[p.stem] = receipt
    if len(sources) != 36:
        raise ValueError("Exact original source budget required")
    metadata, errors, samples = {}, {}, []
    for symbol in original.SYMBOLS:
        try:
            raw = [sources[k + "-" + symbol] for k in ("spot_meta", "perp_meta")]
            if any(r["status"] != 200 for r in raw):
                raise ValueError("Metadata source unavailable")
            metadata[symbol] = partial_metadata(*(json.loads(r["raw_response_utf8"]) for r in raw), symbol)
        except (ValueError, KeyError, TypeError) as error:
            errors[symbol] = str(error)[:160]
    for sample in range(1, original.SAMPLES + 1):
        markets = {}
        for symbol in original.SYMBOLS:
            if symbol not in metadata:
                markets[symbol] = {"status": "METADATA_UNAVAILABLE"}
                continue
            m = metadata[symbol]
            spot, zero_spot = without_empty_levels(sources[f"sample-{sample}-spot_book-{symbol}"], "spot")
            perp, zero_perp = without_empty_levels(sources[f"sample-{sample}-perp_book-{symbol}"], "perp")
            value = original.analyze_pair(m, spot, perp)
            value["zero_available_size_levels_ignored"] = {"spot": zero_spot, "perp": zero_perp}
            for row in value["quotes"]:
                unknown = [v for v in m["unverified_size_limits"] if
                           row["mode"] == "paired" or v.startswith(row["mode"] + ".")]
                row["unverified_size_limits"] = unknown
                row["all_published_size_rules_verified"] = not unknown
                if unknown and row["status"] == "STATIC_QUOTE_AVAILABLE":
                    row["status"] = "STATIC_QUOTE_PARTIAL_SIZE_LIMITS"
            markets[symbol] = value
        samples.append({"sample": sample, "markets": markets})
    if hashlib.sha256(original.base._path(root).read_bytes()).hexdigest() != before:
        raise ValueError("Original audit modified")
    return {"analysis": "POST_CAPTURE_PARTIAL_METADATA_PRICE_QUOTE_SUPPLEMENT_V1",
            "analyzed_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_fingerprint": original.base.digest({"original": original.fingerprint(),
                "supplement": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}),
            "original_journal_head": prior["journal_head"], "original_db_sha256_unchanged": before,
            "archived_receipt_sha256": hashes, "new_public_requests": 0,
            "post_capture_descriptive_analysis": True, "metadata": metadata,
            "metadata_errors": errors, "samples": samples,
            "personal_fee_profile_status": "UNVERIFIED_NOT_QUERIED", "actual_fills_measured": False,
            "execution_authorized": False, "profitability_proven": False,
            "sufficient_to_lower_slippage": False}
