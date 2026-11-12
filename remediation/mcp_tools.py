"""Register against the host's existing FastMCP-compatible instance."""
from __future__ import annotations


def register_tools(mcp, service, *, include_legacy=True):
    """Do not register include_legacy=True alongside an existing tool of that name."""
    names = ["ingest_build", "list_failures", "get_failure_context", "list_evidence", "get_evidence",
             "search_verified_fixes", "validate_patch", "get_validation", "record_resolution"]
    if include_legacy:
        names.append("get_serenity_report")
    for name in names:
        mcp.tool()(getattr(service, name))
