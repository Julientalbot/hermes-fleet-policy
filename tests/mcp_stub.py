"""A real streamable-HTTP MCP server for the watch tests (127.0.0.1 only, needs mcp>=2 + uvicorn).

It exposes ``COMPOSIO_MULTI_EXECUTE_TOOL`` and answers with a Composio-shaped envelope whose
provider payload is a JSON *string* (the case ``watch`` must see through). The items are the ids
listed in ``items_file`` (a JSON array, re-read on every call); every call also returns a fresh
random field, like real APIs do. Each call is appended to ``log_file`` when given.

In-process: ``with serve(items_file) as url: ...``.
Subprocess: ``python mcp_stub.py <port-file> <items-file> <log-file>``.
"""
from __future__ import annotations

import contextlib
import json
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Iterator, Optional


def build_server(items_file: Path, log_file: Optional[Path] = None):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("watch-stub")

    def multi_execute(tools: list, thought: str = "") -> str:
        """Run tools (stub)."""
        if log_file is not None:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"tools": tools}) + "\n")
        ids = json.loads(Path(items_file).read_text())
        if ids == "FAIL":
            return json.dumps({"successful": False, "error": "stub: connection expired",
                               "data": None})
        messages = [{"messageId": i, "sender": f"sender-{i}@example.com", "subject": f"subject {i}",
                     "etag": uuid.uuid4().hex} for i in ids]
        inner = json.dumps({"messages": messages, "nextPageToken": uuid.uuid4().hex})
        return json.dumps({"successful": True, "error": None, "log_id": uuid.uuid4().hex,
                           "data": {"results": [{"tool_slug": tools[0].get("tool_slug"),
                                                 "response": {"successful": True, "data": inner}}]}})

    server.add_tool(multi_execute, name="COMPOSIO_MULTI_EXECUTE_TOOL")
    return server


def _uvicorn(items_file: Path, log_file: Optional[Path]):
    import uvicorn

    app = build_server(items_file, log_file).streamable_http_app()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(app, log_level="error", lifespan="on")
    return uvicorn.Server(config), sock


@contextlib.contextmanager
def serve(items_file: Path, log_file: Optional[Path] = None) -> Iterator[str]:
    Path(items_file).write_text("[]") if not Path(items_file).exists() else None
    server, sock = _uvicorn(items_file, log_file)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def main() -> None:
    port_file, items_file, log_file = (Path(a) for a in sys.argv[1:4])
    if not items_file.exists():
        items_file.write_text("[]")
    server, sock = _uvicorn(items_file, log_file)
    port_file.write_text(str(sock.getsockname()[1]))
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
