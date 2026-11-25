"""Create disjoint, reproducible Food-11 workload manifests from local images."""

import argparse
import hashlib
import json
import re
from pathlib import Path


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
NAMED_CATEGORIES = (
    "bread", "dairyproduct", "dessert", "egg", "friedfood", "meat",
    "noodlespasta", "rice", "seafood", "soup", "vegetablefruit",
)


def category_for(relative_path):
    label = relative_path.parts[0] if len(relative_path.parts) > 1 else relative_path.stem.split("_", 1)[0]
    if len(relative_path.parts) > 1:
        normalized = re.sub(r"[^a-z]", "", label.lower())
        if normalized in NAMED_CATEGORIES:
            return NAMED_CATEGORIES.index(normalized)
    match = re.fullmatch(r"(?:class[_-]?)?(\d{1,2})", label, flags=re.IGNORECASE)
    if not match or not 0 <= int(match[1]) <= 10:
        raise ValueError(f"Expected a Food-11 class ID (0–10) for {relative_path.as_posix()}")
    return int(match[1])


def manifest(root, split, phase, release, seed, samples_per_category, seen):
    split_path = root / split
    if not split_path.is_dir():
        raise ValueError(f"Missing Food-11 split directory: {split_path}")
    groups = {category: [] for category in range(11)}
    for path in split_path.rglob("*"):
        if path.suffix.lower() in IMAGE_EXTENSIONS and path.is_file():
            category = category_for(path.relative_to(split_path))
            groups[category].append(path)

    images = []
    for category, paths in groups.items():
        # Hash ranks depend only on the seed and relative path, so directory order does not matter.
        ranked = sorted(paths, key=lambda path: (
            hashlib.sha256(f"{seed}:{path.relative_to(root).as_posix()}".encode()).digest(),
            path.relative_to(root).as_posix(),
        ))
        selected = 0
        for path in ranked:
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if not content or digest in seen:
                continue
            seen.add(digest)
            images.append({
                "image_id": digest, "category_id": category, "bytes": len(content),
                "relative_path": path.relative_to(root).as_posix(),
            })
            selected += 1
            if selected == samples_per_category:
                break
        if selected != samples_per_category:
            raise ValueError(f"{split} class {category}: found only {selected} usable unique images")

    return {
        "schema_version": 1, "dataset": "Food-11", "source_release": release, "split": split,
        "phase": phase, "seed": seed, "samples_per_category": samples_per_category, "images": images,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("food11_directory", type=Path)
    parser.add_argument("output_directory", type=Path)
    parser.add_argument("--release", required=True, help="source URL and archive revision or hash")
    parser.add_argument("--seed", type=int, default=5306)
    parser.add_argument("--per-category", type=int, default=20)
    args = parser.parse_args()
    if args.per_category < 1 or not args.release.strip():
        parser.error("a nonempty release and a positive per-category count are required")
    seen = set()
    try:
        pilot = manifest(args.food11_directory, "validation", "pilot", args.release, args.seed, args.per_category, seen)
        final = manifest(args.food11_directory, "evaluation", "final", args.release, args.seed, args.per_category, seen)
        args.output_directory.mkdir(parents=True, exist_ok=True)
        for result in (pilot, final):
            payload = (json.dumps(result, indent=2) + "\n").encode()
            path = args.output_directory / f"{result['phase']}_food11.json"
            path.write_bytes(payload)
            print(f"{path}: {len(result['images'])} images, SHA-256 {hashlib.sha256(payload).hexdigest()}")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
