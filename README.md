# GourmetGram canary evaluation gate

CSCI 5306 project extension for the [GourmetGram MLOps teaching pipeline](https://teaching-on-testbeds.github.io/mlops-chi/). The gate compares a candidate in Canary with the current Production release and returns PASS, FAIL, or HOLD with reasons and evidence. The collector and validator use Python 3.10 or later and the standard library.

**Start here for review**

- [Measured local pilot](PILOT.md) shows how requests moved through GourmetGram and links the results to the raw data.
- [Chameleon implementation plan](IMPLEMENTATION_PLAN.md) explains how the Python collector and gate will fit into the existing Kubernetes, Argo, and MLflow setup.
- [Case study protocol](CASE_STUDY.md) describes the dataset, repeated trials, and analysis for the research report.

- `collect.py` sends matched multipart image requests to two direct GourmetGram endpoints and captures timing, response status, and outcome. It checks `/version` before and after each pair.
- `gate.py` reads a paired request CSV and trial metadata. It validates the workload and measurements, computes the metrics, applies the policy, and writes a JSON decision record to standard output.
- `build_workload.py` selects separate, class-balanced pilot and final Food-11 samples from a local dataset. It records each image's hash, size and source-relative path.
- `examples.py` creates synthetic fixtures for checking validator behavior.

## Collect a paired run

Supply two direct service base URLs in a local `targets.json`. Get `model_version` from each service's `/version` and record each resolved deployment image separately. The collector checks the model version and retains the operator-supplied image identity. The following values illustrate the shape:

```json
{
  "production": {"url": "http://127.0.0.1:18730", "model_version": "v1", "image_id": "resolved-baseline-id"},
  "canary": {"url": "http://127.0.0.1:18731", "model_version": "v2", "image_id": "resolved-candidate-id"}
}
```

```sh
python3 collect.py targets.json runs/workloads/pilot_food11.json data/Food-11 runs/pilot-01 --source chameleon
python3 gate.py runs/pilot-01/trial.json runs/pilot-01/requests.csv > runs/pilot-01/decision.json
```

The collector verifies the bytes of every input against the manifest, reuses one shuffled image sequence and paced schedule, and bounds in-flight requests separately per endpoint. It records missed starts while keeping endpoint failures in their own outcome fields. A 2xx response needs a valid GourmetGram category in the prediction button. The original `/predict` route writes each upload to disk. Use isolated replicas with temporary upload storage for collection. Keep target configuration and working data outside the public evidence set.

## Prepare the dataset

The [Chameleon Food-11 preparation guide](https://teaching-on-testbeds.github.io/eval-offline-chi/#prepare-data) shows how to obtain and organize the source splits. Point the sampler at an extracted copy:

```sh
python3 build_workload.py data/Food-11 runs/workloads --release '<source URL and archive revision/hash>'
```

The sampler defaults to a seed of `5306` and 20 images per class per split, for 220 pilot and 220 final inputs. It supports `validation` and `evaluation` splits with label-prefixed filenames such as `0_123.jpg` or class directories such as `class_00/` and `Bread/`. It excludes byte-identical duplicates within and between samples. The collector records the manifest SHA-256 with each trial. Keep image archives outside Git.

## Evidence contract (version 1)

`trial.json` contains one trial ID, source (`synthetic`, `local`, or `chameleon`), phase (`fixture`, `pilot`, or `final`), SHA-256 of the workload manifest, target starts per second, measurement window in seconds, and time elapsed since gate launch at evidence handoff. It records a model version, container image identity, and the measurement window start relative to gate launch for each deployment. The example generator produces a complete JSON file. Use one trial folder per pair and keep the scenario assignment as separate study metadata.

Each row in `requests.csv` records a single scheduled start. Use the following column order:

```csv
environment,slot,image_id,scheduled_ms,started_ms,latency_ms,outcome,http_status
```

Use `production` and `canary` and zero-based slot IDs. Pair the environments using identical slots, image IDs and scheduled offsets. The evaluator checks the offsets against the declared pacing rate. Times use a monotonic clock and are relative to that environment's measurement start. Start each attempted request in the measurement window. Allow the final in-flight cohort to finish within the gate deadline and record its terminal outcome. Capture higher precision than whole milliseconds for very fast requests. Count each scheduled request once, with retries disabled. Outcomes are `success` (2xx and valid prediction response), `http_error` (non-2xx), `invalid_response` (2xx with unusable response), `timeout`, `network_error`, or `not_sent` (the generator could not start a scheduled request). A `not_sent` row carries empty start, latency and HTTP status fields. Timeouts and network errors carry empty HTTP status fields.

The validator computes p95 over successful response latencies using the nearest-rank method. HTTP errors, invalid responses, timeouts, and network errors count toward the failure rate. The denominator is actual requests started. Successful requests per second is successful outcomes from that cohort divided by its scheduling window, including responses that finished during the bounded drain. The output JSON preserves source metadata, input hashes, policy settings, per-environment summaries, and checks. Decimal values are serialized as strings.

An absent, invalid, or insufficient evidence set produces HOLD before threshold comparisons. The prototype requires 200 completed HTTP responses per environment and at least one successful response per environment. Timeouts and network errors count as failures but are excluded from that HTTP-response minimum. Generator starts must be within 100 ms of their schedule. Actual offered counts must be within 5% of the target and of each other (using the target as the denominator). The five-minute deadline includes warm-up and request drain. The workflow will need to enforce that deadline around the entire gate, including collection failures, validation and decision storage.

For valid trials, FAIL means at least one of the agreed latency, failure-rate, or successful-request-rate thresholds was violated. PASS permits the existing promotion workflow once integrated. A FAIL or HOLD keeps the current Production version in place.

Run `python3 -m unittest -v` to check the policy and manifest sampler. Synthetic fixtures generated by `examples.py` use `source: synthetic` and `phase: fixture`. The measured pilot uses `source: local` and `phase: pilot`. Chameleon collection and Argo/MLflow integration are subsequent work.
