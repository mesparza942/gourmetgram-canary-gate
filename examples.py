"""Generate clearly marked synthetic request logs to exercise the validator."""

import argparse
import csv
import json
from pathlib import Path

from gate import ENVIRONMENTS, FIELDS


def example_trial(case):
    slots = 240
    trial = {
        "schema_version": 1, "trial_id": f"synthetic-{case}", "source": "synthetic",
        "phase": "fixture", "workload_sha256": "0" * 64, "measurement_s": 120,
        "target_rps": 2, "gate_elapsed_s": 152,
        "deployments": {
            env: {"model_version": f"example-{env}", "image_id": f"example-{env}", "measurement_start_s": 30}
            for env in ENVIRONMENTS
        },
    }
    rows = []
    for environment in ENVIRONMENTS:
        for slot in range(slots):
            scheduled = slot * 500
            outcome, status, started, latency = "success", 200, scheduled, 100 + slot % 10
            if environment == "canary":
                if case == "delay":
                    latency += 250
                if case == "error" and slot % 20 == 0:
                    outcome, status = "http_error", 500
                if case == "incomplete" and slot >= 180:
                    outcome, status, started, latency = "not_sent", "", "", ""
            rows.append(dict(zip(FIELDS, (
                environment, slot, f"example-image-{slot % 11:02d}", scheduled,
                started, latency, outcome, status,
            ))))
    return trial, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    for case in ("healthy", "delay", "error", "incomplete"):
        trial, rows = example_trial(case)
        path = args.directory / case
        path.mkdir(parents=True, exist_ok=True)
        (path / "trial.json").write_text(json.dumps(trial, indent=2) + "\n", encoding="utf-8")
        with (path / "requests.csv").open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    main()
