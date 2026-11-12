"""Bounded HTTPS Jenkins transport. Never follows artifact-supplied URLs."""
from __future__ import annotations

import base64
import json
import ssl
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .security import safe_relative


class FetchError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Project:
    jenkins_url: str
    jobs: tuple[str, ...]
    report_prefix: str = "target/site/serenity"
    framework: str = "serenity-java"
    ca_bundle: str | None = None
    username_env: str = "JENKINS_USER"
    token_env: str = "JENKINS_TOKEN"
    redaction_patterns: tuple[str, ...] = ()
    max_artifact_bytes: int = 10_000_000
    max_build_bytes: int = 100_000_000
    max_artifacts: int = 2000
    related_tests: tuple[str, ...] = ()
    validation_job: str | None = None
    validation_commit_parameter: str = "REMEDIATION_COMMIT"
    validation_tests_parameter: str = "REMEDIATION_TESTS"
    allowed_serenity_versions: tuple[str, ...] = ()

    def __post_init__(self):
        u = urlsplit(self.jenkins_url)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("Configure a credential-free HTTPS Jenkins URL")
        safe_relative(self.report_prefix)
        if min(self.max_artifact_bytes, self.max_build_bytes, self.max_artifacts) < 1:
            raise ValueError("Artifact limits must be positive")


class Jenkins:
    def __init__(self, project: Project, username: str, token: str):
        self.project = project
        self.auth = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        context = ssl.create_default_context(cafile=project.ca_bundle)
        self.opener = build_opener(NoRedirect(), HTTPSHandler(context=context))

    def request(self, relative: str, *, method="GET", data=None, max_bytes=None):
        if relative.startswith(("/", "http:" , "https:")) or ".." in relative.split("/"):
            raise ValueError("Only internally constructed Jenkins paths are permitted")
        url = self.project.jenkins_url.rstrip("/") + "/" + relative
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        if data is not None:
            data = urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                limit = max_bytes or self.project.max_artifact_bytes
                payload = response.read(limit + 1)
                if len(payload) > limit:
                    raise FetchError("Artifact exceeds configured byte limit")
                return payload, dict(response.headers)
        except HTTPError as exc:
            raise FetchError(f"Jenkins HTTP {exc.code}; redirects are not followed") from None
        except (URLError, TimeoutError, OSError):
            raise FetchError("Jenkins transport failure; check connectivity and corporate CA") from None

    def job_path(self, job):
        if job not in (*self.project.jobs, self.project.validation_job):
            raise ValueError("Job is not configured for this project")
        if any(not part or part in (".", "..") for part in job.split("/")):
            raise ValueError("Invalid Jenkins job path")
        return "/".join("job/" + quote(part, safe="") for part in job.split("/"))

    def build_info(self, job, build):
        if isinstance(build, bool) or not isinstance(build, int) or build < 1:
            raise ValueError("A positive, immutable build number is required")
        body, _ = self.request(f"{self.job_path(job)}/{build}/api/json?tree=number,building,result,artifacts[fileName,relativePath]")
        return json.loads(body)

    def artifact(self, job, build, relative):
        path = safe_relative(relative)
        body, _ = self.request(f"{self.job_path(job)}/{int(build)}/artifact/{quote(path, safe='/')}")
        return body

    def collect(self, job, build):
        info = self.build_info(job, build)
        if info.get("building"):
            raise FetchError("Build is still running; evidence is not immutable yet")
        prefix = self.project.report_prefix.rstrip("/") + "/"
        candidates = [a["relativePath"] for a in info.get("artifacts", [])
                      if a.get("relativePath", "").startswith(prefix)]
        # JSON outcomes first. HTML and text evidence are retained for fallback/context.
        candidates.sort(key=lambda name: (not name.endswith(".json"), name))
        warnings, files, used = [], {}, 0
        if len(candidates) > self.project.max_artifacts:
            warnings.append("Artifact count limit reached; extraction is incomplete")
        for path in candidates[:self.project.max_artifacts]:
            name = path[len(prefix):]
            if not name.lower().endswith((".json", ".html", ".txt", ".log", ".xml")):
                continue  # Images require separate authorized viewing and visual redaction.
            try:
                payload = self.artifact(job, build, path)
                used += len(payload)
                if used > self.project.max_build_bytes:
                    warnings.append("Build byte limit reached; extraction is incomplete")
                    break
                files[name] = payload.decode("utf-8", errors="strict")
            except (FetchError, UnicodeError, ValueError):
                warnings.append(f"Could not retrieve text artifact: {name}")
        if not files and warnings:
            raise FetchError("No text artifacts could be retrieved within configured limits")
        return files, warnings, info

    def start_validation(self, commit, test_ids):
        job = self.project.validation_job
        if not job:
            raise ValueError("No validation job configured")
        _, headers = self.request(self.job_path(job) + "/buildWithParameters", method="POST", data={
            self.project.validation_commit_parameter: commit,
            self.project.validation_tests_parameter: ",".join(test_ids),
        })
        location = headers.get("Location") or headers.get("location", "")
        import re
        match = re.search(r"/queue/item/(\d+)/?$", location)
        if not match:
            raise FetchError("Jenkins did not return a queue identifier")
        return {"queue": int(match[1]), "job": job}

    def poll_validation(self, handle):
        if not handle.get("build"):
            body, _ = self.request(f"queue/item/{int(handle['queue'])}/api/json")
            data = json.loads(body)
            if data.get("cancelled"):
                return {"status": "error", "reason": "Jenkins queue item cancelled"}
            if not data.get("executable"):
                return {"status": "running"}
            handle["build"] = int(data["executable"]["number"])
        build = handle["build"]
        info = self.build_info(handle["job"], build)
        if info.get("building"):
            return {"status": "running", "build": build}
        # This manifest is emitted by the configured trusted CI harness, not Copilot.
        body = self.artifact(handle["job"], build, "remediation-validation.json")
        receipt = json.loads(body)
        receipt.update(status="complete", build=build, jenkins_result=info.get("result"))
        return receipt
