from __future__ import annotations

import json
import os

from .jenkins import Jenkins, Project
from .runners import JenkinsRunner, LocalRunner
from .service import RemediationService


def load_service(path, *, review_authorizer=None):
    with open(path, encoding="utf-8") as stream:
        config = json.load(stream)
    projects, clients, runners = {}, {}, {}
    for name, entry in config["projects"].items():
        entry = dict(entry)
        local = entry.pop("local_runner", None)
        for key in ("jobs", "related_tests", "redaction_patterns", "allowed_serenity_versions"):
            if key in entry:
                entry[key] = tuple(entry[key])
        cfg = Project(**entry)
        projects[name] = cfg
        username, token = os.environ.get(cfg.username_env), os.environ.get(cfg.token_env)
        if username and token:
            client = clients[name] = Jenkins(cfg, username, token)
            if cfg.validation_job:
                runners[(name, "jenkins")] = JenkinsRunner(client)
        if local:
            runners[(name, "local")] = LocalRunner(name, patterns=cfg.redaction_patterns, **local)
    return RemediationService(config["database"], projects, clients, runners, review_authorizer)
