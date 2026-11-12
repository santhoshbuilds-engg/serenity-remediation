"""Only administrator-configured commands/jobs can execute tests."""
from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

from .parsers import FAILED, parse_artifact
from .security import fingerprint, redact, safe_relative

COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
SELECTOR = re.compile(r"[A-Za-z_$][\w.$]*(?:#[A-Za-z_$][\w$]*)?\Z")


def validate_inputs(commit, test_ids):
    if not COMMIT.fullmatch(commit):
        raise ValueError("patch_ref must be a full immutable lowercase Git commit SHA")
    if not test_ids or len(test_ids) > 100 or any(not SELECTOR.fullmatch(t) for t in test_ids):
        raise ValueError("Use 1–100 Java class or class#method selectors, without wildcards/options")


class JenkinsRunner:
    def __init__(self, client):
        self.client = client

    def start(self, commit, test_ids):
        validate_inputs(commit, test_ids)
        return self.client.start_validation(commit, test_ids)

    def poll(self, handle):
        return self.client.poll_validation(handle)


class LocalRunner:
    """Exports an exact commit to a temporary directory, never changes the user's checkout.

    argv must write fresh Serenity outcomes to {report_dir}. It must use {tests}.
    No shell, model-supplied commands, or persistent process output is exposed.
    """
    def __init__(self, project, repo, argv, *, timeout=900, patterns=(), max_parallel=1):
        if not argv or not any("{tests}" in arg for arg in argv) or not any("{report_dir}" in arg for arg in argv):
            raise ValueError("Configured command must use {tests} and {report_dir}")
        self.project, self.repo, self.argv = project, str(Path(repo).resolve()), tuple(argv)
        self.timeout, self.patterns = timeout, tuple(patterns)
        self.results, self.lock = {}, threading.Lock()
        self.slots = threading.BoundedSemaphore(max_parallel)

    def start(self, commit, test_ids):
        validate_inputs(commit, test_ids)
        handle = {"id": uuid.uuid4().hex}
        with self.lock:
            self.results[handle["id"]] = {"status": "running"}
        threading.Thread(target=self._execute, args=(handle, commit, list(test_ids)), daemon=True).start()
        return handle

    def poll(self, handle):
        with self.lock:
            return self.results.get(handle["id"], {"status": "error", "reason": "Local runner restarted; rerun validation"}).copy()

    def _execute(self, handle, commit, test_ids):
        try:
            with self.slots:
                result = self._run(commit, test_ids)
        except Exception as exc:
            result = {"status": "error", "reason": type(exc).__name__}
        with self.lock:
            self.results[handle["id"]] = result

    def _run(self, commit, test_ids):
        actual = subprocess.check_output(["git", "-C", self.repo, "rev-parse", commit + "^{commit}"], timeout=30).decode().strip()
        if actual != commit:
            raise ValueError("Commit mismatch")
        archive = subprocess.check_output(["git", "-C", self.repo, "archive", "--format=zip", commit], timeout=60)
        with tempfile.TemporaryDirectory(prefix="remediation-") as work:
            root = Path(work)
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                for item in bundle.infolist():
                    safe_relative(item.filename.rstrip("/"))
                    if (item.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("Symlinks in exported repositories are not supported")
                bundle.extractall(root)
            # Sibling temporary output directory cannot contain committed stale reports.
            with tempfile.TemporaryDirectory(prefix="remediation-outcomes-") as output:
                argv = [arg.replace("{tests}", ",".join(test_ids)).replace("{report_dir}", output) for arg in self.argv]
                completed = subprocess.run(argv, cwd=root, shell=False, timeout=self.timeout,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                tests, fingerprints, warnings, incomplete = [], [], [], False
                for file in sorted(Path(output).glob("*.json")):
                    if file.name in {"serenity-summary.json", "remediation-build.json"}:
                        continue
                    parsed = parse_artifact(file.read_text(encoding="utf-8"), file.name)
                    incomplete = incomplete or not parsed.recognized
                    warnings.extend(parsed.warnings)
                    for test in parsed.tests:
                        test = redact(test, self.patterns)
                        tests.append({"selector": test["selector"], "status": test["status"]})
                        if test["status"] in FAILED:
                            fingerprints.append(fingerprint(self.project, test))
                return {"status": "complete", "commit": commit, "tests": tests,
                        "failure_fingerprints": fingerprints, "exit_code": completed.returncode,
                        "warnings": warnings, "extraction_complete": bool(tests) and not incomplete}
