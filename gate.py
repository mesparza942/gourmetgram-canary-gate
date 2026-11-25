"""Evaluate one paired trial against the GourmetGram promotion policy."""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path


ENVIRONMENTS = ("production", "canary")
FIELDS = (
    "environment", "slot", "image_id", "scheduled_ms", "started_ms",
    "latency_ms", "outcome", "http_status",
)
RESPONSES = {"success", "http_error", "invalid_response"}
OUTCOMES = RESPONSES | {"timeout", "network_error", "not_sent"}
POLICY = {
    "version": "m2-pilot-v1",
    "p95_latency_ratio_max": Decimal("1.10"),
    "failure_rate_increase_max": Decimal("0.01"),
    "failure_rate_max": Decimal("0.02"),
    "successful_rps_ratio_min": Decimal("0.90"),
    "min_http_responses": 200,
    "timeout_s": 300,
    "offered_rate_tolerance": Decimal("0.05"),
    "max_start_lag_ms": 100,
}


class EvidenceError(ValueError):
    """Evidence cannot support a promotion decision."""


def number(value, label):
    try:
        value = Decimal(str(value))
    except InvalidOperation as exc:
        raise EvidenceError(f"{label} must be a number") from exc
    if not value.is_finite() or value < 0:
        raise EvidenceError(f"{label} must be finite and nonnegative")
    return value


def identifier(value, label):
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{label} is required")
    return value


def trial_config(trial):
    if type(trial["schema_version"]) is not int or trial["schema_version"] != 1:
        raise EvidenceError("schema_version must be 1")
    identifier(trial["trial_id"], "trial_id")
    if trial["source"] not in ("synthetic", "local", "chameleon"):
        raise EvidenceError("source must be synthetic, local, or chameleon")
    if trial["phase"] not in ("fixture", "pilot", "final"):
        raise EvidenceError("phase must be fixture, pilot, or final")
    if (trial["source"] == "synthetic") != (trial["phase"] == "fixture"):
        raise EvidenceError("synthetic evidence must use the fixture phase")
    if trial["phase"] == "final" and trial["source"] != "chameleon":
        raise EvidenceError("the final phase requires Chameleon evidence")
    if "version_checks" in trial:
        version_checks = trial["version_checks"]
        if not isinstance(version_checks, dict) or any(version_checks.get(env) is not True for env in ENVIRONMENTS):
            raise EvidenceError("deployment version changed or could not be verified after collection")
    manifest_hash = identifier(trial["workload_sha256"], "workload_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
        raise EvidenceError("workload_sha256 must be a lowercase SHA-256 hash")
    window = number(trial["measurement_s"], "measurement_s")
    rate = number(trial["target_rps"], "target_rps")
    elapsed = number(trial["gate_elapsed_s"], "gate_elapsed_s")
    if window == 0 or rate == 0:
        raise EvidenceError("measurement_s and target_rps must be positive")
    if elapsed >= POLICY["timeout_s"]:
        raise EvidenceError("gate reached its five-minute timeout")
    offsets = {}
    for environment in ENVIRONMENTS:
        deployment = trial["deployments"][environment]
        identifier(deployment["model_version"], f"{environment} model_version")
        identifier(deployment["image_id"], f"{environment} image_id")
        offset = offsets[environment] = number(deployment["measurement_start_s"], "measurement_start_s")
        if offset + window > elapsed:
            raise EvidenceError(f"{environment} measurement window did not finish")
    slots = window * rate
    if slots < 1 or slots != slots.to_integral_value():
        raise EvidenceError("target_rps × measurement_s must give a whole number of scheduled requests")
    return window, rate, int(slots), {environment: elapsed - offset for environment, offset in offsets.items()}


def read_requests(reader, window, rate, slots, elapsed_by_environment):
    if tuple(reader.fieldnames or ()) != FIELDS:
        raise EvidenceError("request CSV header does not match the schema")
    requests = {environment: {} for environment in ENVIRONMENTS}
    for row in reader:
        try:
            if None in row or None in row.values():
                raise EvidenceError("wrong number of fields")
            environment = row["environment"]
            if environment not in requests:
                raise EvidenceError("unknown environment")
            environment_requests = requests[environment]
            slot = int(row["slot"])
            if not 0 <= slot < slots or slot in environment_requests:
                raise EvidenceError(f"invalid or repeated {environment} slot {slot}")
            image_id = identifier(row["image_id"], "image_id")
            scheduled = number(row["scheduled_ms"], "scheduled_ms")
            if scheduled >= window * 1000 or abs(scheduled - slot * 1000 / rate) > 1:
                raise EvidenceError(f"slot {slot} does not match the planned schedule")
            latency = request_latency(row, scheduled, window, elapsed_by_environment[environment])
            environment_requests[slot] = (image_id, scheduled, row["outcome"], latency)
        except (ValueError, KeyError) as exc:
            raise EvidenceError(f"CSV line {reader.line_num}: {exc}") from exc

    if any(len(rows) != slots for rows in requests.values()):
        raise EvidenceError("both environments must record one outcome for every scheduled slot")
    for slot, row in requests["production"].items():
        if row[:2] != requests["canary"][slot][:2]:
            raise EvidenceError(f"image or schedule differs across environments at slot {slot}")
    return requests


def request_latency(row, scheduled, window, elapsed):
    outcome = row["outcome"]
    if outcome not in OUTCOMES:
        raise EvidenceError(f"unknown outcome {outcome!r}")
    status = row["http_status"]
    if outcome in RESPONSES:
        status_code = int(status)
        if not 200 <= status_code <= 599:
            raise EvidenceError("HTTP status must be a final response code")
        if (200 <= status_code < 300) != (outcome in {"success", "invalid_response"}):
            raise EvidenceError("outcome is inconsistent with HTTP status")
    elif status:
        raise EvidenceError(f"{outcome} cannot carry an HTTP status")
    if outcome == "not_sent":
        if row["started_ms"] or row["latency_ms"]:
            raise EvidenceError("not_sent cannot carry start or latency values")
        return None
    started = number(row["started_ms"], "started_ms")
    latency = number(row["latency_ms"], "latency_ms")
    if started < scheduled or started >= window * 1000:
        raise EvidenceError("request started outside its scheduled measurement window")
    if started - scheduled > POLICY["max_start_lag_ms"]:
        raise EvidenceError("load generator exceeded the start-lag tolerance")
    if latency == 0 or started + latency > elapsed * 1000:
        raise EvidenceError("request latency is zero or finishes beyond gate_elapsed_s")
    return latency


def summarize(rows, window):
    counts = Counter(row[2] for row in rows.values())
    sent = len(rows) - counts["not_sent"]
    successes = counts["success"]
    latencies = sorted(row[3] for row in rows.values() if row[2] == "success")
    return {
        "scheduled": len(rows),
        "offered": sent,
        "successes": successes,
        "http_responses": sum(counts[outcome] for outcome in RESPONSES),
        "outcomes": {outcome: counts[outcome] for outcome in sorted(OUTCOMES)},
        "offered_rps": sent / window,
        "p95_latency_ms": latencies[(95 * successes + 99) // 100 - 1] if successes else None,
        "failure_rate": Decimal(sent - successes) / sent if sent else None,
        "successful_rps": successes / window,
    }


def evaluate(trial, reader):
    """Return a decision, reasons, policy settings, and the available measurements."""
    result = {key: trial.get(key) for key in (
        "trial_id", "source", "phase", "workload_sha256", "deployments",
        "measurement_s", "target_rps", "gate_elapsed_s",
    )} if isinstance(trial, dict) else {}
    result.update(decision="HOLD", reasons=[], policy=POLICY.copy(), metrics={}, checks=[])
    try:
        window, rate, slots, elapsed_by_environment = trial_config(trial)
        requests = read_requests(reader, window, rate, slots, elapsed_by_environment)
    except (EvidenceError, KeyError, TypeError, csv.Error, UnicodeError, ArithmeticError) as exc:
        result["reasons"].append(f"Invalid or incomplete evidence: {exc}")
        return result

    metrics = result["metrics"] = {
        environment: summarize(requests[environment], window) for environment in ENVIRONMENTS
    }
    production, canary = (metrics[environment] for environment in ENVIRONMENTS)
    reasons = result["reasons"]
    tolerance = slots * POLICY["offered_rate_tolerance"]
    for environment, values in metrics.items():
        if values["http_responses"] < POLICY["min_http_responses"]:
            reasons.append(f"{environment} recorded fewer than 200 completed HTTP responses")
        if abs(values["offered"] - slots) > tolerance:
            reasons.append(f"{environment} missed the target offered request rate")
        if values["p95_latency_ms"] is None:
            reasons.append(f"{environment} had no successful response to measure p95 latency")
    if abs(canary["offered"] - production["offered"]) > tolerance:
        reasons.append("canary and production offered request rates did not match")
    if reasons:
        return result

    # Compare counts on identical windows and cross-multiply rates to preserve inclusive boundaries.
    p_sent, c_sent = production["offered"], canary["offered"]
    p_failures = p_sent - production["successes"]
    c_failures = c_sent - canary["successes"]
    failures_ok = (
        c_failures <= c_sent * POLICY["failure_rate_max"]
        and c_failures * p_sent <= p_failures * c_sent + POLICY["failure_rate_increase_max"] * p_sent * c_sent
    )
    throughput_ok = canary["successes"] >= production["successes"] * POLICY["successful_rps_ratio_min"]
    p95_limit = production["p95_latency_ms"] * POLICY["p95_latency_ratio_max"]
    check_inputs = (
        ("p95_latency_ms", "<=", canary["p95_latency_ms"],
         p95_limit, canary["p95_latency_ms"] <= p95_limit),
        ("failure_rate", "<=", canary["failure_rate"],
         min(production["failure_rate"] + POLICY["failure_rate_increase_max"], POLICY["failure_rate_max"]),
         failures_ok),
        ("successful_rps", ">=", canary["successful_rps"],
         production["successful_rps"] * POLICY["successful_rps_ratio_min"], throughput_ok),
    )
    for metric, operator, observed, limit, passed in check_inputs:
        result["checks"].append({
            "metric": metric, "operator": operator, "canary": observed, "limit": limit, "passed": passed,
        })
        if not passed:
            reasons.append(f"canary {metric} violated its limit")
    result["decision"] = "FAIL" if reasons else "PASS"
    if not reasons:
        reasons.append("All policy checks passed")
    return result


def reject_nonfinite(value):
    raise EvidenceError(f"non-finite JSON number {value}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path, help="trial metadata JSON")
    parser.add_argument("requests", type=Path, help="paired request CSV")
    args = parser.parse_args()
    hashes = {}
    try:
        trial_bytes = args.trial.read_bytes()
        hashes["trial_sha256"] = hashlib.sha256(trial_bytes).hexdigest()
        trial = json.loads(trial_bytes, parse_float=Decimal, parse_constant=reject_nonfinite)
        request_bytes = args.requests.read_bytes()
        hashes["requests_sha256"] = hashlib.sha256(request_bytes).hexdigest()
        reader = csv.DictReader(io.StringIO(request_bytes.decode("utf-8"), newline=""), strict=True)
        result = evaluate(trial, reader)
    except (OSError, ValueError) as exc:
        result = {"decision": "HOLD", "policy": POLICY, "reasons": [f"Could not read evidence: {exc}"]}
    result["evidence"] = hashes
    # Decimal strings preserve threshold arithmetic and document the reported precision.
    print(json.dumps(result, indent=2, default=str, allow_nan=False))
    return {"PASS": 0, "FAIL": 1, "HOLD": 2}[result["decision"]]


if __name__ == "__main__":
    sys.exit(main())
