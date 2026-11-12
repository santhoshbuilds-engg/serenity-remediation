import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from remediation.jenkins import FetchError, Jenkins, NoRedirect, Project
from remediation.mcp_tools import register_tools
from remediation.runners import LocalRunner
from remediation.service import RemediationService
import test_core as fixtures
from test_core import BASE, PATCH, outcome


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.client = Jenkins(Project("https://jenkins.example/context", ("folder/suite",), validation_job="validate"), "user", "secret")

    def test_nested_job_encoding(self):
        self.assertEqual(self.client.job_path("folder/suite"), "job/folder/job/suite")

    def test_no_redirect_forwarding(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.example"))

    def test_collect_fetch_failure_is_explicit(self):
        self.client.build_info = lambda job, build: {"artifacts": [{"relativePath": "target/site/serenity/one.json"}]}
        def fail(*args):
            raise FetchError("Missing")
        self.client.artifact = fail
        with self.assertRaises(FetchError):
            self.client.collect("folder/suite", 1)

    def test_pending_build_not_cached(self):
        self.client.build_info = lambda job, build: {"building": True}
        with self.assertRaises(FetchError):
            self.client.collect("folder/suite", 1)

    def test_queue_handle_retains_build(self):
        requests = []
        def request(path):
            requests.append(path)
            return b'{"executable":{"number":42}}', {}
        self.client.request = request
        self.client.build_info = lambda job, build: {"building": True}
        handle = {"queue": 9, "job": "validate"}
        self.client.poll_validation(handle)
        self.client.poll_validation(handle)
        self.assertEqual(len(requests), 1)
        self.assertEqual(handle["build"], 42)


class LocalIntegrationTests(unittest.TestCase):
    def test_exact_commit_export_preserves_working_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            script = repo / "emit.py"
            original = "import pathlib,sys\npathlib.Path(sys.argv[1], 'test.json').write_text(" + repr(outcome("SUCCESS")) + ")\n"
            script.write_text(original, encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "emit.py"], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
            commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            # The user's working change must not be executed or overwritten.
            script.write_text("raise RuntimeError('uncommitted change')", encoding="utf-8")
            runner = LocalRunner("P", repo, [sys.executable, "emit.py", "{report_dir}", "{tests}"])
            receipt = runner._run(commit, ["example.UiTest#find"])
            self.assertEqual(receipt["exit_code"], 0)
            self.assertTrue(receipt["extraction_complete"])
            self.assertEqual(receipt["commit"], commit)
            self.assertEqual(receipt["tests"][0]["status"], "SUCCESS")
            self.assertIn("uncommitted change", script.read_text())


class ExtraGateTests(unittest.TestCase):
    setUp = fixtures.CoreTests.setUp
    tearDown = fixtures.CoreTests.tearDown
    ingest = fixtures.CoreTests.ingest
    failure = fixtures.CoreTests.failure
    receipt = fixtures.CoreTests.receipt
    validation = fixtures.CoreTests.validation
    verified_case = fixtures.CoreTests.verified_case
    def test_incomplete_receipt_does_not_pass(self):
        fid = self.failure()
        receipt = self.receipt(PATCH)
        receipt["extraction_complete"] = False
        self.assertEqual(self.validation(fid, PATCH, "local", receipt)["status"], "inconclusive")

    def test_jenkins_success_cannot_override_failed_exit(self):
        fid = self.failure()
        receipt = self.receipt(PATCH)
        receipt["exit_code"] = 1
        self.assertEqual(self.validation(fid, PATCH, "jenkins", receipt)["status"], "failed")

    def test_unknown_json_cannot_be_silently_ignored(self):
        result = self.ingest({"one.json": outcome(), "other.json": '{"newSchema":true}'})
        self.assertEqual(result["extraction_status"], "partial_extraction")

    def test_manifest_version_used(self):
        self.s.projects["P"] = Project("https://jenkins.example", ("suite",), allowed_serenity_versions=("4.2.0",))
        result = self.ingest({"one.json": outcome(), "remediation-build.json": '{"serenity_version":"4.2.0"}'})
        self.assertEqual(result["extraction_status"], "complete")

    def test_evidence_is_scoped_to_run(self):
        first = self.failure()
        second = self.failure()
        evidence = self.s.get_failure_context(first)["failure"]["evidence_id"]
        with self.assertRaises(ValueError):
            self.s.get_evidence(second, evidence)

    def test_fts_retrieval_and_restart(self):
        _, case = self.verified_case()
        result = self.ingest({"new.json": outcome(message="Missing element save button")})
        fid = self.s.list_failures(result["id"])["items"][0]["id"]
        self.s = RemediationService(self.s.store.path, {"P": self.cfg})
        self.assertEqual(self.s.search_verified_fixes(fid)["verified_fixes"][0]["id"], case["id"])

    def test_mcp_registration_preserves_names(self):
        class Host:
            def __init__(self):
                self.methods = {}
            def tool(self):
                def register(fn):
                    self.methods[fn.__name__] = fn
                    return fn
                return register
        host = Host()
        register_tools(host, self.s)
        self.assertEqual(len(host.methods), 10)
        self.assertIn("get_serenity_report", host.methods)


if __name__ == "__main__":
    unittest.main()
