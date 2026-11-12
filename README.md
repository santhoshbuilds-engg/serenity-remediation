# Serenity remediation for Copilot + Jenkins MCP

**New here? Start with [SETUP.md](SETUP.md)** for an ordered installation, Jenkins configuration, verification, troubleshooting, and AI handoff guide. [Integration details](docs/integration.md) describe each MCP tool and the validation contract; [Copilot workflow](docs/copilot-workflow.md) provides instructions for diagnosing a failure.

A working Python core that adds structured failure evidence, repair memory, and validation gates to an existing MCP server. It makes **no model API calls**. Copilot inspects your test repository and proposes edits; this package retrieves evidence, runs configured validation, and remembers reviewed outcomes.

The repository was created from the supplied screenshots, not an export of your original server. The visible legacy signature is preserved, but the original module cannot be replaced byte-for-byte. Production compatibility must be checked using your actual Serenity version and sanitized reports.

## How the pieces fit together

```text
Jenkins test build -> archived Serenity outcomes -> existing Jenkins MCP server
                                                    |
                              remediation package -> SQLite evidence and repair memory
                                                    |
Copilot -> MCP tools -> inspect test code -> patch commit -> local + Jenkins validation
                                                    |
                                      human review -> verified reusable case
```

The MCP server calls Python functions in this package. It does not host a model. The database stores redacted text and review history; the test repository remains separate. The Jenkins validation job must be configured by your team and must check out the exact commit under review.

## Repository map

| Location | Purpose |
|---|---|
| [SETUP.md](SETUP.md) | Step-by-step setup and checks for a person or AI agent |
| [config.example.json](config.example.json) | Example project, Jenkins, and local-runner configuration |
| [remediation/config.py](remediation/config.py) | Loads configuration and constructs adapters |
| [remediation/mcp_tools.py](remediation/mcp_tools.py) | Registers tools on your existing MCP server |
| [remediation/parsers.py](remediation/parsers.py) | Serenity outcome parsers |
| [remediation/service.py](remediation/service.py) | Ingestion, validation, and repair-memory workflow |
| [remediation/receipt.py](remediation/receipt.py) | Trusted CI receipt generator |
| [tests](tests) | Synthetic fixtures and tests; not a substitute for your reports |

## Start here

Requires Python 3.11+ with SQLite FTS5 and Git. The core has no third-party runtime dependencies. Your existing MCP server supplies its own MCP package.

```console
python -m unittest discover -s tests -v
python -m pip install -e .
```

Copy `config.example.json` to `config.local.json`, set your jobs, repository, validation command, corporate CA file, and credential environment variables. Use a dedicated local data directory with access restricted to your user/service account. Do not commit secrets or production evidence.

Attach to your **existing** server, after it creates `mcp`:

```python
from remediation.config import load_service
from remediation import register_tools

service = load_service("config.local.json")
register_tools(mcp, service)
# Keep your existing mcp.run() and other Jenkins tools.
```

Remove the old Serenity registration to avoid a duplicate `get_serenity_report` tool. Alternatively, pass `include_legacy=False` and keep the old wrapper while migrating callers. No standalone MCP transport or new MCP SDK version is imposed.

The initial configuration deliberately has no human review adapter. Patches can be generated and validated, but cannot become verified reusable cases until a trusted review source is connected. See [integration](docs/integration.md).

## What is implemented

- JSON-first Serenity ingestion, conservative HTML fallback, explicit extraction status, immutable build caching, nested failed steps, parameter data, stack frames, and failure grouping.
- Paginated failures and evidence. Full redacted text stays in SQLite; summaries are bounded. Screenshots stay in Jenkins and are not automatically sent to the model.
- SQLite exact-fingerprint and FTS5 retrieval restricted by project and configured framework/version family; at most three verified cases plus recent rejected/unverified cases.
- Exact-commit validation using a temporary local Git export or a parameterized Jenkins job. Local runs are asynchronous; Jenkins runs are polled. Neither changes your current checkout.
- Baseline reproduction, complete executed scope, both local and Jenkins validation, repair-specific evidence, and human approval gates for promotion.
- Two distinct patch commits per failure group per ingestion. Repeated validation of the same commit does not consume a new attempt. A refreshed run starts a new budget and is recorded separately.
- Revocation, attempt history, basic ingestion/validation metrics, and a compatibility summary wrapper.

The server does not decide that a UI element is semantically correct. A unique match alone is insufficient: the reviewer must inspect the intended element and the asserted postcondition. Similarly, a passing test is not proof that a product defect has been fixed.

## Workflow

1. Ingest the failed build; inspect completeness before diagnosis.
2. Select a failure group, retrieve relevant evidence and prior verified cases.
3. Inspect the exact baseline test commit locally; reproduce the failure with `validate_patch` using that baseline commit.
4. Create a `codex/` repair branch in the **test repository**, preserving user changes. Establish intent from code, fixtures, DOM, and any supplied specification.
5. Make the smallest patch. Commit it locally so validation has an immutable SHA. Record an `attempted` resolution immediately.
6. Validate that SHA locally and in Jenkins, including the failing selector and configured related tests. Push the branch to an allowed remote when needed for Jenkins to fetch it.
7. Review the patch, evidence, and results. Record acceptance through your trusted human review integration; only then can the case become verified.
8. Stop after one revision, or earlier when intent/evidence is insufficient. Record rejected/unresolved outcomes rather than making tests less strict.

See [the Copilot workflow](docs/copilot-workflow.md) for a reusable instruction file and [integration](docs/integration.md) for tool contracts and CI setup.

## Limits that matter

- The JSON adapter follows the documented per-test shape. An optional `allowed_serenity_versions` setting flags unknown/unapproved versions as partial. Many versions omit a version field, so record it in the build manifest or leave the allowlist empty during fixture qualification.
- HTML fallback supports explicit `data-result` test containers with `test-case-title` or `data-test-name`. It does **not** claim support for every historical Serenity layout. All archived report HTML files are inspected; it does not crawl arbitrary HTML links or authenticated iframe wrappers. Archive the report directory in Jenkins.
- Per-test JSON is authoritative when present. Archive one clean report directory per build and emit an accurate `outcome_count`; mixed/stale reports cannot be inferred reliably from appearance.
- Validation currently accepts Java `class#method` selectors (or class selectors when outcome mapping supports them). Cucumber feature/scenario selection needs a runner-specific adapter and real fixtures; no guessed mapping is used.
- Local Git exports do not include uncommitted files, submodule contents, ignored configuration, or Git metadata. Provide environment configuration through the trusted runner. Use a CI adapter for repositories requiring those features.
- The local command is administrator-configured and executes test code with the server user's privileges. It is not an OS sandbox. Configure a dedicated test runner account/container where appropriate.
- Built-in redaction handles common credential fields, headers, bearer tokens, and emails. Supply organization-specific patterns and fixture checks; it is not a universal PII detector. DOM text can contain sensitive data and screenshots are not redacted here.
- Copilot model tokens, one-diagnosis/one-revision reasoning discipline, and source edits live in Copilot. The server enforces patch-attempt limits but cannot meter hidden model calls.
- No deployment, production Jenkins run, Java compilation, or validation against your private reports has been performed here.

Serenity's [official outcome documentation](https://serenity-bdd.github.io/docs/ai/ai_test_results) is the reference for the JSON adapter. The outcome format is not a public versioned schema; approve adapters with your own fixture set before rollout.
