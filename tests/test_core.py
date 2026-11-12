import json
import tempfile
import unittest
from pathlib import Path

from remediation.jenkins import FetchError, Jenkins, Project
from remediation.parsers import html_report, json_report
from remediation.security import redact, safe_relative
from remediation.service import RemediationService


BASE = "a" * 40
PATCH = "b" * 40


def outcome(status="FAILURE", message="Missing element", name="one", **extra):
    return json.dumps(dict(title=name, testCaseName="example.UiTest", methodName="find",
                           result=status, testFailureCause={"errorType": "NoSuchElementException", "message": message},
                           testSteps=[{"description": "Open", "children": [{"description": "Find", "result": status,
                                       "exception": {"errorType": "NoSuchElementException", "message": message}}]}], **extra))


class FakeRunner:
    def __init__(self, receipt):
        self.receipt = receipt

    def start(self, commit, test_ids):
        return {"requested": commit}

    def poll(self, handle):
        return self.receipt


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Project("https://jenkins.example", ("suite",))
        self.s = RemediationService(Path(self.temp.name) / "db.sqlite", {"P": self.cfg})

    def tearDown(self):
        self.temp.cleanup()

    def ingest(self, files=None, **kwargs):
        return self.s.ingest_artifacts("P", "suite", 1, files or {"one.json": outcome(),
            "remediation-build.json": json.dumps({"test_commit": BASE, "outcome_count": 1})}, **kwargs)

    def failure(self):
        run = self.ingest()
        return self.s.list_failures(run["id"])["items"][0]["id"]

    def receipt(self, commit, status="SUCCESS", fingerprints=None, **kwargs):
        return dict(status="complete", commit=commit, tests=[{"selector": "example.UiTest#find", "status": status}],
                    failure_fingerprints=fingerprints or [], exit_code=0, jenkins_result="SUCCESS",
                    extraction_complete=True, **kwargs)

    def validation(self, fid, commit, runner, receipt):
        self.s.runners[("P", runner)] = FakeRunner(receipt)
        record = self.s.validate_patch(fid, commit, ["example.UiTest#find"], runner)
        return self.s.get_validation(record["id"])

    def test_nested_step_and_parameter_identity(self):
        data = json_report(outcome(parameters={"row": 2}), "x.json")
        self.assertEqual(data.tests[0]["step_path"], [0, 0])
        self.assertEqual(data.tests[0]["parameters"], {"row": 2})

    def test_expected_http_error_is_not_failure(self):
        run = self.ingest({"pass.json": outcome("SUCCESS", "HTTP 400 expected")})
        self.assertEqual(run["extraction_status"], "no_failures")

    def test_success_http_with_assertion_is_failure(self):
        run = self.ingest({"fail.json": outcome("FAILURE", "HTTP 200; expected balance 20, got 10")})
        self.assertEqual(run["failure_count"], 1)

    def test_pagination_and_full_evidence(self):
        run = self.ingest({f"{i}.json": outcome(message="x" * 1500, name=str(i)) for i in range(65)})
        ids, cursor = [], ""
        while True:
            page = self.s.list_failures(run["id"], cursor, 20)
            ids += [item["id"] for item in page["items"]]
            self.assertTrue(page["items"][0]["message_truncated"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(set(ids)), 65)
        context = self.s.get_failure_context(ids[0])
        result = self.s.get_evidence(ids[0], context["failure"]["evidence_id"])
        self.assertIn("x" * 1500, result["content"])

    def test_no_rest_errors_does_not_mean_no_failed_tests(self):
        run = self.ingest({"index.html": "<html>Serenity unsupported layout</html>"})
        self.assertEqual(run["extraction_status"], "unsupported_report")

    def test_partial_download_and_malformed_json(self):
        run = self.ingest({"one.json": outcome(), "two.json": "{"}, warnings=["download failed"])
        self.assertEqual(run["extraction_status"], "partial_extraction")

    def test_single_and_multiple_page_html(self):
        block = '<section data-result="FAILURE"><span class="test-case-title">One</span><div class="error-message">No element</div></section>'
        self.assertEqual(len(html_report(block + block, "index.html").tests), 2)
        run = self.ingest({"a.html": block, "b.html": block,
                           "remediation-build.json": '{"outcome_count": 2}'})
        self.assertEqual(run["failure_count"], 2)
        self.assertEqual(run["extraction_status"], "complete")

    def test_json_authoritative_and_dom_not_test(self):
        block = '<section data-result="FAILURE" data-test-name="Fake">ignored</section>'
        run = self.ingest({"a.json": outcome("SUCCESS"), "index.html": block, "dom/snapshot.html": block})
        self.assertEqual(run["failure_count"], 0)

    def test_redaction(self):
        value = redact('{"password":"abc", "nested": {"Authorization": "Bearer xyz"}, "message": "test@example.com"}')
        self.assertNotIn("abc", value)
        self.assertNotIn("xyz", value)
        self.assertNotIn("test@example.com", value)
        self.assertNotIn("mysecret", redact("api_key=mysecret"))

    def test_path_guards(self):
        for path in ("../x", "/x", "C:/secret", "a\\b", "a/../b"):
            with self.assertRaises(ValueError):
                safe_relative(path)
        with self.assertRaises(ValueError):
            Project("http://unsafe", ("suite",))
        with self.assertRaises(ValueError):
            Jenkins(self.cfg, "user", "token").job_path("another-job")

    def test_wrong_commit_and_scope_never_pass(self):
        fid = self.failure()
        result = self.validation(fid, PATCH, "local", self.receipt(BASE))
        self.assertEqual(result["status"], "inconclusive")
        receipt = self.receipt(PATCH)
        receipt["tests"] = [{"selector": "Other#test", "status": "SUCCESS"}]
        result = self.validation(fid, PATCH, "local", receipt)
        self.assertEqual(result["status"], "inconclusive")

    def test_skipped_test_does_not_pass(self):
        fid = self.failure()
        result = self.validation(fid, PATCH, "local", self.receipt(PATCH, "SKIPPED"))
        self.assertEqual(result["status"], "failed")

    def test_flaky_baseline_is_inconclusive(self):
        fid = self.failure()
        result = self.validation(fid, BASE, "local", self.receipt(BASE))
        self.assertEqual(result["status"], "inconclusive")

    def test_two_patch_budget(self):
        fid = self.failure()
        self.validation(fid, PATCH, "local", self.receipt(PATCH))
        self.validation(fid, "c" * 40, "local", self.receipt("c" * 40))
        with self.assertRaises(ValueError):
            self.validation(fid, "d" * 40, "local", self.receipt("d" * 40))

    def verified_case(self, authorize=True, repair_type="locator", evidence=None):
        fid = self.failure()
        fp = self.s.store.get("failures", fid)["fingerprint"]
        baseline = self.validation(fid, BASE, "local", self.receipt(BASE, "FAILURE", [fp]))
        local = self.validation(fid, PATCH, "local", self.receipt(PATCH))
        ci = self.validation(fid, PATCH, "jenkins", self.receipt(PATCH))
        if authorize:
            self.s.review_authorizer = lambda review: "human-review-record-42"
        record = self.s.record_resolution(fid, PATCH, [baseline["id"], local["id"], ci["id"]], "accepted",
            "Old locator changed", "Only for widget v2", repair_type,
            evidence if evidence is not None else {"intended_element": "save button", "unique_match": "DOM check: 1", "postcondition": "Saved message asserted"})
        return fid, record

    def test_verified_requires_baseline_both_runners_and_human(self):
        fid, record = self.verified_case()
        self.assertTrue(record["verified"])
        self.assertEqual(len(self.s.search_verified_fixes(fid)["verified_fixes"]), 1)

    def test_no_human_approval_cannot_promote(self):
        _, record = self.verified_case(authorize=False)
        self.assertFalse(record["verified"])

    def test_wrong_element_or_product_defect_cannot_promote(self):
        _, record = self.verified_case(evidence={"unique_match": "1"})
        self.assertFalse(record["verified"])
        _, record = self.verified_case(repair_type="product_defect")
        self.assertFalse(record["verified"])

    def test_revoke_removes_searchable_fix(self):
        fid, record = self.verified_case()
        self.s.record_resolution(fid, PATCH, [], "revoked", "Wrong element", "Do not reuse")
        self.assertEqual(self.s.search_verified_fixes(fid)["verified_fixes"], [])

    def test_cross_project_search_isolation(self):
        fid, _ = self.verified_case()
        self.s.projects["Q"] = self.cfg
        run = self.s.ingest_artifacts("Q", "suite", 2, {"one.json": outcome()})
        other = self.s.list_failures(run["id"])["items"][0]["id"]
        self.assertEqual(self.s.search_verified_fixes(other)["verified_fixes"], [])

    def test_version_mismatch_marks_partial(self):
        self.s.projects["P"] = Project("https://jenkins.example", ("suite",), allowed_serenity_versions=("4.2.0",))
        run = self.ingest()
        self.assertEqual(run["extraction_status"], "partial_extraction")

    def test_fetch_failure_and_cache(self):
        self.assertEqual(self.s.ingest_build("P", "suite", 1)["extraction_status"], "fetch_failure")
        run = self.ingest()
        cached = self.s.ingest_build("P", "suite", 1)
        self.assertEqual(cached["id"], run["id"])
        self.assertTrue(cached["cached"])


if __name__ == "__main__":
    unittest.main()
