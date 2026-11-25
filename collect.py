"""Collect paired request evidence from GourmetGram's image prediction endpoint."""

import argparse
import csv
import hashlib
import http.client
import json
import mimetypes
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from gate import ENVIRONMENTS, FIELDS, POLICY


PREDICTION = re.compile(rb"\s*<button\b[^>]*>([^<>]+)</button>\s*", re.IGNORECASE)
FOOD_LABELS = {
    b"Bread", b"Dairy product", b"Dessert", b"Egg", b"Fried food", b"Meat",
    b"Noodles/Pasta", b"Rice", b"Seafood", b"Soup", b"Vegetable/Fruit",
}
RESPONSE_BYTES = 16_384


def load_workload(manifest_path, image_root, phase, seed):
    contents = manifest_path.read_bytes()
    manifest = json.loads(contents)
    if manifest["schema_version"] != 1 or manifest["phase"] != phase:
        raise ValueError("workload manifest version or phase does not match the trial")
    if phase == "final" and manifest["dataset"] != "Food-11":
        raise ValueError("the final protocol uses a Food-11 manifest")
    root = image_root.resolve()
    images = {}
    for image in manifest["images"]:
        path = (root / image["relative_path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("image path is outside the image root")
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != image["image_id"] or len(payload) != image["bytes"] or not payload:
            raise ValueError(f"image does not match its manifest: {image['relative_path']}")
        if digest in images:
            raise ValueError("workload manifest contains a duplicate image")
        content_type = mimetypes.guess_type(path.name)[0]
        if content_type not in {"image/jpeg", "image/png"}:
            raise ValueError(f"unsupported image type: {image['relative_path']}")
        images[digest] = (path.suffix.lower(), content_type, payload)
    if not images:
        raise ValueError("workload manifest contains no images")
    order = list(images)
    random.Random(seed).shuffle(order)
    return hashlib.sha256(contents).hexdigest(), images, order


def http_call(endpoint, method, path, timeout, body=None, headers=None):
    connection_class = http.client.HTTPSConnection if endpoint.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(endpoint.hostname, endpoint.port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        with connection.getresponse() as response:
            return response.status, response.read(RESPONSE_BYTES + 1)
    finally:
        connection.close()


def check_version(endpoint, expected, timeout):
    status, body = http_call(endpoint, "GET", "/version", timeout)
    version = json.loads(body) if status == 200 and len(body) <= RESPONSE_BYTES else None
    if not isinstance(version, dict) or version.get("model_version") != expected:
        raise ValueError("/version did not match the expected model version")


def upload(endpoint, image, filename, timeout):
    suffix, content_type, payload = image
    boundary = uuid.uuid4().hex
    prefix = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}{suffix}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("ascii")
    body = prefix + payload + f"\r\n--{boundary}--\r\n".encode("ascii")
    status, body = http_call(endpoint, "POST", "/predict", timeout, body, {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    if not 200 <= status < 300:
        return "http_error", status
    match = PREDICTION.fullmatch(body) if len(body) <= RESPONSE_BYTES else None
    outcome = "success" if match and match[1].strip() in FOOD_LABELS else "invalid_response"
    return outcome, status


def record_request(endpoint, image, filename, row, planned, start, end, timeout, permits):
    try:
        began = time.perf_counter()
        if began >= end or (began - planned) * 1000 > POLICY["max_start_lag_ms"]:
            return
        row["started_ms"] = f"{(began - start) * 1000:.6f}"
        try:
            row["outcome"], row["http_status"] = upload(endpoint, image, filename, timeout)
        except TimeoutError:
            row["outcome"] = "timeout"
        except (OSError, http.client.HTTPException):
            row["outcome"] = "network_error"
        row["latency_ms"] = f"{(time.perf_counter() - began) * 1000:.6f}"
    finally:
        permits.release()


def collect_window(endpoints, images, order, seconds, settings, prefix):
    rate, workers, timeout = settings["rate"], settings["workers"], settings["timeout"]
    permits = {environment: threading.BoundedSemaphore(workers) for environment in ENVIRONMENTS}
    rows = []
    pending = []
    with ThreadPoolExecutor(max_workers=workers * len(endpoints)) as pool:
        start = time.perf_counter()
        end = start + seconds
        for slot in range(seconds * rate):
            planned = start + slot / rate
            while (remaining := planned - time.perf_counter()) > 0:
                time.sleep(remaining)
            image_id = order[slot % len(order)]
            # Alternate submission order to avoid giving one endpoint a systematic head start.
            order_this_slot = ENVIRONMENTS if slot % 2 == 0 else ENVIRONMENTS[::-1]
            for environment in order_this_slot:
                row = dict(zip(FIELDS, (
                    environment, slot, image_id, f"{slot * 1000 / rate:.6f}", "", "", "not_sent", "",
                )))
                rows.append(row)
                if not permits[environment].acquire(blocking=False):
                    continue
                pending.append(pool.submit(
                    record_request, endpoints[environment], images[image_id], f"{prefix}-{environment}-{slot}",
                    row, planned, start, end, timeout, permits[environment],
                ))
        for future in pending:
            future.result()
        if (remaining := end - time.perf_counter()) > 0:
            time.sleep(remaining)
    return start, rows


def collect(args):
    config = json.loads(args.targets.read_bytes())
    endpoints = {}
    deployments = {}
    for environment in ENVIRONMENTS:
        target = config[environment]
        endpoint = urlsplit(target["url"])
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
                or endpoint.password or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment):
            raise ValueError(f"{environment} requires a plain HTTP or HTTPS service base URL")
        deployments[environment] = {key: target[key] for key in ("model_version", "image_id")}
        if any(not isinstance(value, str) or not value.strip() for value in deployments[environment].values()):
            raise ValueError(f"{environment} needs a model version and deployment image ID")
        endpoints[environment] = endpoint
    if endpoints["production"] == endpoints["canary"]:
        raise ValueError("use distinct direct endpoints for production and canary")

    digest, images, order = load_workload(args.manifest, args.image_root, args.phase, args.seed)
    for environment, endpoint in endpoints.items():
        try:
            check_version(endpoint, deployments[environment]["model_version"], args.timeout)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise ValueError(f"{environment} version preflight failed: {exc}") from exc

    args.output.mkdir(parents=True, exist_ok=False)
    prefix = uuid.uuid4().hex
    settings = {"rate": args.rate, "workers": args.workers, "timeout": args.timeout}
    timestamp = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    if args.warmup:
        collect_window(endpoints, images, order, args.warmup, settings, f"{prefix}-warmup")
    measurement_start, rows = collect_window(endpoints, images, order, args.seconds, settings, prefix)
    version_checks = {}
    for environment, endpoint in endpoints.items():
        deployments[environment]["measurement_start_s"] = f"{measurement_start - started:.9f}"
        try:
            check_version(endpoint, deployments[environment]["model_version"], args.timeout)
            version_checks[environment] = True
        except (OSError, ValueError, http.client.HTTPException):
            version_checks[environment] = False
    trial = {
        "schema_version": 1, "trial_id": args.output.name, "source": args.source, "phase": args.phase,
        "started_at_utc": timestamp, "workload_sha256": digest, "workload_seed": args.seed,
        "target_rps": args.rate, "warmup_s": args.warmup, "measurement_s": args.seconds,
        "gate_elapsed_s": f"{time.perf_counter() - started:.9f}", "deployments": deployments,
        "version_checks": version_checks, "collector": settings,
    }
    with (args.output / "requests.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: (row["environment"], row["slot"])))
    (args.output / "trial.json").write_text(json.dumps(trial, indent=2) + "\n", encoding="utf-8")
    print(f"Captured {len(rows)} request records in {args.output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("targets", "manifest", "image_root", "output"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--source", choices=("local", "chameleon"), required=True)
    parser.add_argument("--phase", choices=("pilot", "final"), default="pilot")
    parser.add_argument("--rate", type=int, default=2, help="scheduled requests per second per endpoint")
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--workers", type=int, default=4, help="maximum in-flight requests per endpoint")
    parser.add_argument("--timeout", type=int, default=3, help="HTTP socket timeout in seconds")
    parser.add_argument("--seed", type=int, default=5306)
    args = parser.parse_args()
    if (min(args.rate, args.seconds, args.workers, args.timeout) < 1 or args.warmup < 0
            or args.seconds + args.warmup + 3 * args.timeout >= POLICY["timeout_s"]):
        parser.error("specify positive workload settings with enough time for the HTTP timeouts inside the gate deadline")
    if args.phase == "final" and args.source != "chameleon":
        parser.error("the final phase uses Chameleon deployments")
    try:
        collect(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
