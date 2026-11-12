"""Dependency-free remediation core; attach tools to your existing MCP instance."""

from .service import RemediationService
from .mcp_tools import register_tools

__all__ = ["RemediationService", "register_tools"]
