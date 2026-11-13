"""Create deterministic, bucket-stratified validation and holdout files from ch04 labels."""
import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tests/data/eval_ch04.jsonl"
VALIDATION = ROOT / "tests/data/eval_ch11_validation.jsonl"
TEST = ROOT / "tests/data/eval_ch11_test.jsonl"
MANIFEST = ROOT / "tests/data/eval_ch11_split_manifest.json"
SALT = "mewhelp-ch11-split-v1"
VALIDATION_FRACTION = 0.8


def main() -> None:
    raw = SOURCE.read_bytes()
    samples = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    buckets: dict[str, list[dict]] = {}
    for sample in samples:
        buckets.setdefault(sample["bucket"], []).append(sample)
    validation, test = [], []
    for bucket, rows in sorted(buckets.items()):
        ordered = sorted(rows, key=lambda row: hashlib.sha256(
            f"{SALT}:{row['id']}".encode("utf-8")).hexdigest())
        cut = max(1, min(len(ordered) - 1, round(len(ordered) * VALIDATION_FRACTION)))
        validation.extend(ordered[:cut])
        test.extend(ordered[cut:])
    for rows in (validation, test):
        rows.sort(key=lambda row: row["id"])
    for path, rows in ((VALIDATION, validation), (TEST, test)):
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")
    manifest = {
        "schema_version": 1, "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": hashlib.sha256(raw).hexdigest(), "salt": SALT,
        "validation_fraction_per_bucket": VALIDATION_FRACTION,
        "validation": {"path": str(VALIDATION.relative_to(ROOT)), "size": len(validation),
                       "sha256": hashlib.sha256(VALIDATION.read_bytes()).hexdigest()},
        "test": {"path": str(TEST.relative_to(ROOT)), "size": len(test),
                 "sha256": hashlib.sha256(TEST.read_bytes()).hexdigest()},
        "bucket_counts": {bucket: {"validation": sum(x["bucket"] == bucket for x in validation),
                                   "test": sum(x["bucket"] == bucket for x in test)}
                          for bucket in sorted(buckets)},
        "note": "Derived split; historical ch04 report used the full source set. Collect new labels for a fully unseen final test.",
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"validation={len(validation)} test={len(test)} manifest={MANIFEST.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
