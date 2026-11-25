import csv
import io
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from build_workload import category_for, manifest
from examples import example_trial
from gate import FIELDS, evaluate


def decision(trial, rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    buffer.seek(0)
    return evaluate(trial, csv.DictReader(buffer, strict=True))


class GateTests(unittest.TestCase):
    def test_demo_scenarios(self):
        for case, expected, reason in (
            ("healthy", "PASS", "All policy checks passed"),
            ("delay", "FAIL", "canary p95_latency_ms violated its limit"),
            ("error", "FAIL", "canary failure_rate violated its limit"),
            ("incomplete", "HOLD", "canary missed the target offered request rate"),
        ):
            with self.subTest(case=case):
                result = decision(*example_trial(case))
                self.assertEqual(result["decision"], expected)
                self.assertIn(reason, result["reasons"])
        result = decision(*example_trial("healthy"))
        self.assertEqual(result["metrics"]["production"]["p95_latency_ms"], Decimal(109))
        self.assertEqual(result["metrics"]["canary"]["successful_rps"], 2)

    def test_inclusive_relative_failure_and_latency_limits(self):
        trial, rows = example_trial("healthy")
        trial.update(measurement_s=100)
        rows = [row for row in rows if row["slot"] < 200]
        canary = [row for row in rows if row["environment"] == "canary"]
        for row in canary:
            row["latency_ms"] = "119.9"  # 109 × 1.10
        for row in canary[:2]:
            row.update(outcome="http_error", http_status=500)
        self.assertEqual(decision(trial, rows)["decision"], "PASS")
        canary[2].update(outcome="http_error", http_status=500)
        self.assertIn("canary failure_rate violated its limit", decision(trial, rows)["reasons"])

    def test_absolute_failure_ceiling_applies_even_with_unhealthy_production(self):
        trial, rows = example_trial("healthy")
        for row in rows:
            if row["slot"] < 5:
                row.update(outcome="http_error", http_status=500)
        result = decision(trial, rows)
        self.assertEqual(result["decision"], "FAIL")
        self.assertIn("canary failure_rate violated its limit", result["reasons"])

    def test_only_same_paired_workload_is_valid(self):
        trial, rows = example_trial("healthy")
        rows[-1]["image_id"] = "different-image"
        result = decision(trial, rows)
        self.assertEqual(result["decision"], "HOLD")
        self.assertIn("image or schedule differs", result["reasons"][0])
        rows.pop()
        self.assertIn("every scheduled slot", decision(trial, rows)["reasons"][0])

    def test_generator_lag_and_overall_timeout_hold(self):
        trial, rows = example_trial("healthy")
        rows[0]["started_ms"] = 101
        self.assertIn("start-lag tolerance", decision(trial, rows)["reasons"][0])
        trial["gate_elapsed_s"] = 300
        self.assertIn("five-minute timeout", decision(trial, rows)["reasons"][0])

    def test_timeouts_are_failures_and_inadequate_http_samples_take_precedence(self):
        trial, rows = example_trial("healthy")
        for row in rows:
            if row["environment"] == "canary" and row["slot"] < 41:
                row.update(outcome="timeout", http_status="", latency_ms=1500)
        result = decision(trial, rows)
        self.assertEqual(result["decision"], "HOLD")
        self.assertEqual(result["metrics"]["canary"]["offered"], 240)
        self.assertEqual(result["metrics"]["canary"]["http_responses"], 199)
        self.assertEqual(result["metrics"]["canary"]["failure_rate"], Decimal(41) / 240)
        self.assertEqual(result["checks"], [])

    def test_version_change_after_collection_holds_promotion(self):
        trial, rows = example_trial("healthy")
        trial["version_checks"] = {"production": True, "canary": False}
        result = decision(trial, rows)
        self.assertEqual(result["decision"], "HOLD")
        self.assertIn("deployment version changed", result["reasons"][0])


class WorkloadTests(unittest.TestCase):
    def test_named_classes_from_food11_archive(self):
        self.assertEqual(category_for(Path("Dairy product/95.jpg")), 1)
        self.assertEqual(category_for(Path("Noodles-Pasta/137.jpg")), 6)
        self.assertEqual(category_for(Path("Vegetable-Fruit/96.jpg")), 10)

    def test_manifests_are_balanced_reproducible_and_exclude_cross_split_duplicates(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for split in ("validation", "evaluation"):
                for category in range(11):
                    folder = root / split / f"class_{category:02d}"
                    folder.mkdir(parents=True)
                    (folder / "duplicate.jpg").write_bytes(f"common {category}".encode())
                    (folder / "unique.jpg").write_bytes(f"{split} {category}".encode())
            seen = set()
            pilot = manifest(root, "validation", "pilot", "fixture", 5306, 1, seen)
            final = manifest(root, "evaluation", "final", "fixture", 5306, 1, seen)
            self.assertEqual(pilot, manifest(root, "validation", "pilot", "fixture", 5306, 1, set()))
            self.assertEqual({image["category_id"] for image in pilot["images"]}, set(range(11)))
            self.assertEqual({image["category_id"] for image in final["images"]}, set(range(11)))
            self.assertEqual(len(seen), 22)


if __name__ == "__main__":
    unittest.main()
