#!/usr/bin/env python3
"""Start the local newline-delimited STDIO MCP façade.

Runtime configuration is supplied by the embedding process; this entry point
only wires the protocol stream and never selects an executable or provider.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.workflow_mcp import WorkflowMCPServer  # noqa: E402


TRANSPORT_TRACE_ENV = "RESEARCH_WORKFLOW_MCP_TRACE_PATH"


def _configure_utf8_stdio() -> None:
    """Make the JSON-RPC wire encoding independent of the Windows locale.

    Codex launches stdio MCP servers as child processes rather than console
    applications.  On Windows that can leave Python's text streams using the
    active ``gbk`` code page, which corrupts non-ASCII request arguments and
    emits non-UTF-8 JSON-RPC responses.  MCP stdio is a UTF-8 wire protocol,
    so configure both directions at the launcher boundary before importing
    any workflow state.
    """

    for stream in (sys.stdin, sys.stdout):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="strict", newline="\n")


def main() -> int:
    """Run the Product MCP STDIO boundary."""

    _configure_utf8_stdio()
    raw_trace_path = os.environ.get(TRANSPORT_TRACE_ENV)
    trace_path = Path(raw_trace_path) if raw_trace_path and raw_trace_path.strip() else None
    try:
        server = WorkflowMCPServer(transport_trace_path=trace_path)
    except ValueError:
        print("workflow MCP transport trace configuration is invalid", file=sys.stderr)
        return 2
    server.serve_stdio()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
