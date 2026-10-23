"""Compare a candidate with its baseline; never publishes anything."""
import argparse
import json
import pathlib
import sys

from app.core.release_gate import evaluate_gate

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "data/ch11/reports/eval_candidates.json"
DEFAULT_THRESHOLDS = ROOT / "data/ch11/release_thresholds.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=pathlib.Path, default=DEFAULT_REPORT)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--thresholds", type=pathlib.Path, default=DEFAULT_THRESHOLDS)
    parser.add_argument("--answer-report", type=pathlib.Path,
                        help="matching eval_ch04.py answer-quality report")
    args = parser.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        thresholds = json.loads(args.thresholds.read_text(encoding="utf-8"))
        answer_report = (json.loads(args.answer_report.read_text(encoding="utf-8"))
                         if args.answer_report else None)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "INCONCLUSIVE", "reasons": [str(exc)], "checks": []},
                         ensure_ascii=False, indent=2))
        return 2
    result = evaluate_gate(report, args.candidate, thresholds, answer_report=answer_report)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
