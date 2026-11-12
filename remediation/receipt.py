"""Trusted CI harness: emit provenance and validation receipts from fresh outcomes.

Use outside Copilot, after the job checks out the requested exact commit and runs tests.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from .parsers import FAILED, parse_artifact
from .security import fingerprint, redact


def make_receipt(project, commit, report_dir, *, exit_code, patterns=()):
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, timeout=30).strip()
    if actual != commit:
        raise ValueError("Requested commit differs from actual checkout")
    tests, failures, incomplete = [], [], False
    for path in sorted(Path(report_dir).glob("*.json")):
        if path.name in {"serenity-summary.json", "remediation-build.json"}:
            continue
        parsed = parse_artifact(path.read_text(encoding="utf-8"), path.name)
        if not parsed.recognized:
            incomplete = True
        for test in parsed.tests:
            test = redact(test, tuple(patterns))
            tests.append({"selector": test["selector"], "status": test["status"]})
            if test["status"] in FAILED:
                failures.append(fingerprint(project, test))
    return {"commit": actual, "tests": tests, "failure_fingerprints": failures,
            "exit_code": exit_code, "extraction_complete": bool(tests) and not incomplete}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--output", default="remediation-validation.json")
    parser.add_argument("--redaction-config", help="JSON array of patterns matching the server project configuration")
    args = parser.parse_args()
    patterns = json.loads(Path(args.redaction_config).read_text()) if args.redaction_config else ()
    receipt = make_receipt(args.project, args.commit, args.report_dir, exit_code=args.exit_code, patterns=patterns)
    Path(args.output).write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    Path(args.report_dir, "remediation-build.json").write_text(json.dumps({
        "test_commit": receipt["commit"], "outcome_count": len(receipt["tests"])
    }), encoding="utf-8")


if __name__ == "__main__":
    main()
