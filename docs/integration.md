# Integration and operations

## Existing Jenkins MCP server

`register_tools(mcp, service)` attaches bound typed methods through `mcp.tool()`. Credentials stay in the Python process and are read from configured environment variables. Project/job allowlists constrain fetch and validation destinations. Requests use HTTPS with normal hostname/CA verification; supply your corporate CA bundle. HTTP redirects are rejected rather than forwarding credentials elsewhere.

If the existing server already has better Jenkins authentication/queue helpers, inject an equivalent client with `collect(job, build)`, `start_validation(commit, test_ids)` and `poll_validation(handle)`. Reuse its authenticated transport instead of maintaining duplicate credentials. The included transport supports API-token Basic authentication; crumb-requiring installations need an adapter using the existing server's CSRF support.

| Tool | Inputs and behavior |
|---|---|
| `ingest_build` | `project, job, build, refresh=False`; returns run ID, counts, warnings, metadata, and extraction status |
| `list_failures` | `run_id, cursor='', limit=20`; maximum page size 100 |
| `get_failure_context` | `failure_id`; bounded cause, source hints, build provenance, available evidence |
| `list_evidence` | `failure_id, cursor='', limit=50`; page through artifact references |
| `get_evidence` | `failure_id, evidence_id, offset=0, limit=8000`; maximum window 16,000 characters |
| `search_verified_fixes` | `failure_id`; exact matches then project/framework-filtered FTS5 matches |
| `validate_patch` | `failure_id, patch_ref, test_ids, runner`; full commit SHA, explicit selectors, configured runner |
| `get_validation` | `validation_id`; polls runner and verifies returned commit and executed scope |
| `record_resolution` | `failure_id, patch_ref, validation_ids, disposition, diagnosis, applicability, repair_type='unknown', evidence={}` |
| `get_serenity_report` | Legacy `project, job_name, build_number`; text summary with run/failure IDs |

Validation is asynchronous. Poll at a moderate interval in the host rather than looping continuously. A local server restart makes unfinished local jobs unverified; rerun them. Jenkins handles persist in SQLite. Queue records can expire before the first poll; poll promptly, and rerun if the queue identifier can no longer be resolved. Once discovered, the build number is retained.

## Build artifacts

Archive the complete `target/site/serenity/**` tree in `post { always { ... } }`, including JSON. Each run must start with a clean output directory. Capture DOM before WebDriver teardown under `evidence/` and retain Serenity screenshots. Link evidence names to test identity where possible. Avoid archiving credentials and unnecessary payloads.

Place `remediation-build.json` in the report directory:

```json
{
  "test_commit": "full-40-or-64-character-lowercase-Git-SHA",
  "outcome_count": 12,
  "environment": "integration",
  "application_version": "release-or-deployment-id",
  "serenity_version": "your-version"
}
```

`outcome_count` is the number of per-test outcome files, not nested steps or data-table rows. The trusted receipt helper writes the commit/count fields; extend it with trusted environment/deployment metadata. Keep the manifest and report from the same execution.

## Local runner

The configured command runs from a temporary Git export. It must support `{tests}` and `{report_dir}` placeholders and emit fresh JSON into that report directory. Customize the Maven Surefire/Failsafe flags for your project. The example's `-Dtest` is a starting point, not a claim that it selects every Serenity runner correctly. On Windows use the actual Maven executable/batch path supported by your environment.

The local runner suppresses console output and reads structured outcomes. Build/configuration failures with no outcomes are inconclusive; use your existing console-log tools to investigate. Tests run with inherited environment variables. No command is accepted from MCP input.

## Jenkins validation job

Use a dedicated trusted pipeline, separate from proposed test-code changes. Configure string parameters `REMEDIATION_COMMIT` and `REMEDIATION_TESTS`. The trusted pipeline must:

1. Validate the SHA format and Java selectors; use a fixed repository URL.
2. Fetch and check out the requested commit exactly. Do not merely run the current branch head.
3. Clean report output, use the approved test environment, execute the requested tests, and retain their exit status.
4. Run the installed **trusted** receipt generator, outside the proposed patch's Python import path:

```console
python -I -m remediation.receipt --project DEMO --commit <actual-requested-SHA> --report-dir target/site/serenity --exit-code <test-command-exit-code>
```

5. Archive root `remediation-validation.json` and `target/site/serenity/**`, even on failure. Preserve a non-success build result when tests fail; do not turn a failed test run green merely to publish reports.

The receipt contains actual commit, executed selectors and outcomes, failure fingerprints, exit code, and extraction completeness. The server additionally requires Jenkins `SUCCESS` for patch validation. Apply the same redaction patterns on server and receipt generator (`--redaction-config`), so baseline fingerprints match.

The receipt helper does not clean directories for you. Run it only against the fresh output of that build. Its installation/pipeline definition must be controlled independently of the patch being evaluated. If application deployment or data has changed between baseline and patch, review the comparison as inconclusive rather than accepting causation from green results alone.

## Human review adapter

Supply `review_authorizer` to `load_service`. It receives the resolution ID, exact patch SHA, validation IDs, diagnosis, applicability, repair type, and evidence. It must check an independent authenticated human review record tied to these values. Return an audit identifier only when accepted; otherwise return `None`.

```python
def authorize_review(request):
    review = company_review_store.lookup(request["resolution_id"])
    if not review or not review.human_approved:
        return None
    if review.commit != request["patch_ref"]:
        return None
    if set(review.validation_ids) != set(request["validation_ids"]):
        return None
    return review.audit_id

service = load_service("config.local.json", review_authorizer=authorize_review)
```

This example names your integration boundary; `company_review_store` is your service, not a bundled dependency. Do not implement approval as `lambda _: True`, and do not trust an agent-provided `reviewer` string.

## Evaluation and rollout

Run `python -m unittest discover -s tests -v` before wiring the server. Start with one API job and one UI job in diagnosis-only use, then enable the configured runners. Import sanitized real fixtures covering each deployed Serenity version before allowing promotion.

Use historical failures with human labels, splitting by failure family and time to avoid training/evaluation leakage. Track parsed versus expected counts, correct diagnoses, top-three retrieval relevance, accepted patches, later revoked repairs, false repairs, and validation duration. `service.metrics()` exposes core event/outcome counts; Copilot token use and held-out labels must come from the host/evaluation dataset, so these values are not fabricated by the server.

Back up the SQLite database using SQLite's backup API or while the service is stopped. WAL files are part of a running database. This v1 schema initializes new databases; later incompatible schema changes require a migration. Evidence retention is currently explicit administrator maintenance, not automatic deletion.
