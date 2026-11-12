"""Conservative Serenity adapters. Unsupported layouts never imply success."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

FAILED = {"FAILURE", "FAILED", "ERROR", "BROKEN", "COMPROMISED"}
PASSED = {"SUCCESS", "PASSED", "PASS"}
OTHER = {"SKIPPED", "IGNORED", "PENDING", "UNDEFINED", "ABORTED"}
KNOWN = FAILED | PASSED | OTHER


@dataclass
class Parsed:
    tests: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recognized: bool = False
    adapter: str = "unknown"


def flatten_steps(steps, parent=()):
    if not isinstance(steps, list):
        return
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        path = (*parent, i)
        yield path, step
        yield from flatten_steps(step.get("children", step.get("steps", [])), path)


def classify(exception: str, message: str) -> str:
    text = exception + " " + message
    if re.search(r"NoSuchElement|InvalidSelector", text, re.I):
        return "locator_candidate"
    if re.search(r"Timeout|StaleElement|ClickIntercepted", text, re.I):
        return "synchronization_or_application"
    if re.search(r"ConnectException|UnknownHost|SSLHandshake|Connection refused", text, re.I):
        return "infrastructure_candidate"
    if re.search(r"Assertion|expected", text, re.I):
        return "assertion_requires_intent"
    return "unknown"


def json_report(content: str, artifact: str) -> Parsed:
    result = Parsed(adapter="serenity-outcome-v1")
    try:
        data = json.loads(content)
    except ValueError:
        result.warnings.append(f"Malformed JSON: {artifact}")
        return result
    if not isinstance(data, dict) or not any(k in data for k in ("testSteps", "testCaseName", "methodName")):
        return result
    status = str(data.get("result", "")).upper()
    if status not in KNOWN:
        result.warnings.append(f"Unsupported outcome status in {artifact}: {status}")
        return result
    result.recognized = True
    steps = list(flatten_steps(data.get("testSteps", [])))
    failing = [(path, step) for path, step in steps if str(step.get("result", "")).upper() in FAILED]
    # Prefer the deepest failed step; preserve all nested steps in stored evidence.
    path, step = max(failing, key=lambda item: len(item[0]), default=((), {}))
    cause = step.get("exception") or data.get("testFailureCause") or {}
    if not isinstance(cause, dict):
        cause = {"message": str(cause)}
    message = str(step.get("errorMessage") or cause.get("message") or data.get("testFailureMessage") or data.get("testFailureSummary") or "")
    exception = str(cause.get("errorType") or cause.get("exceptionClass") or cause.get("type") or data.get("testFailureClassname") or "")
    stack = cause.get("stackTrace") or ""
    stack_text = stack if isinstance(stack, str) else json.dumps(stack, ensure_ascii=False)
    frames = re.findall(r"[\w.$]+\([^()]+\.java:\d+\)", stack_text)
    if isinstance(stack, list):
        frames = [f"{frame.get('declaringClass', '')}.{frame.get('methodName', '')}({frame.get('fileName', '')}:{frame.get('lineNumber', '')})"
                  for frame in stack if isinstance(frame, dict)] or frames
    source = next((frame for frame in frames if not frame.startswith(("java.", "jdk.", "org.junit.", "org.openqa.selenium.", "net.serenitybdd."))), "")
    params = data.get("dataTable", data.get("parameters", data.get("example", {})))
    class_name, method = str(data.get("testCaseName", "")), str(data.get("methodName", ""))
    selector = f"{class_name}#{method}" if class_name and method else ""
    result.tests.append({
        "identity": str(data.get("id") or artifact), "title": str(data.get("title") or data.get("name") or artifact),
        "selector": selector, "parameters": params, "status": status,
        "step": str(step.get("description", "")), "step_path": list(path),
        "exception": exception, "message": message, "source": source, "stack_frames": frames,
        "expected": data.get("expected"), "actual": data.get("actual"),
        "endpoint": (step["restQuery"].get("path") or step["restQuery"].get("url", "")) if isinstance(step.get("restQuery"), dict) else "",
        "locator": str(step.get("locator", data.get("locator", ""))),
        "category": classify(exception, message), "artifact": artifact,
        "serenity_version": str(data.get("serenityVersion", "unknown")),
    })
    if status in FAILED and not failing:
        result.warnings.append(f"No explicit failed step in {artifact}; using outcome-level cause")
    return result


class Node:
    def __init__(self, tag="document", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def text(self):
        return " ".join(c if isinstance(c, str) else c.text() for c in self.children).strip()

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in {"br", "img", "meta", "link", "input", "hr", "source", "wbr", "area", "base", "embed"}:
            self.current = node

    def handle_endtag(self, tag):
        node = self.current
        while node.parent is not None:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, text):
        self.current.children.append(text)


def html_report(content: str, artifact: str) -> Parsed:
    result = Parsed(adapter="serenity-html-explicit-outcome-v1")
    tree = Tree()
    tree.feed(content)
    # Deliberately require explicit test boundaries; an HTTP payload is not an outcome.
    candidates = []
    for node in tree.root.walk():
        status = str(node.attrs.get("data-result", "")).upper()
        if status not in KNOWN:
            continue
        title_node = next((n for n in node.walk() if "test-case-title" in n.attrs.get("class", "")), None)
        if title_node is None and not node.attrs.get("data-test-name"):
            continue
        candidates.append((node, status, title_node))
    for index, (node, status, title_node) in enumerate(candidates):
        # Parent sections wrapping several test outcomes must not duplicate children.
        if any(other is not node and other in list(node.walk()) for other, _, _ in candidates):
            continue
        message = "\n".join(n.text() for n in node.walk() if re.search(r"error-message|failure-detail|exception", n.attrs.get("class", "")))
        result.tests.append({"identity": f"{artifact}#{index}",
                             "title": node.attrs.get("data-test-name") or title_node.text(),
                             "selector": node.attrs.get("data-test-selector", ""),
                             "status": status, "message": message, "exception": "", "source": "",
                             "step": "", "step_path": [], "parameters": {}, "stack_frames": [],
                             "expected": None, "actual": None, "endpoint": "", "locator": "",
                             "category": classify("", message), "artifact": artifact,
                             "serenity_version": "unknown"})
    result.recognized = bool(result.tests)
    if not result.recognized:
        result.warnings.append(f"HTML layout has no supported explicit test outcomes: {artifact}")
    return result


def parse_artifact(content: str, artifact: str) -> Parsed:
    return json_report(content, artifact) if artifact.endswith(".json") else html_report(content, artifact)
