#!/usr/bin/env python3
"""PreToolUse hook: ask the supervisor before the child runs a tool.

The DeepSeek Harness runtime invokes this as an ordinary local process with the
proposed tool call as JSON on stdin. It asks the MCP server over a unix socket
and translates the verdict into the hook wire contract:

    exit 0  -- allow, run the tool
    exit 2  -- block, and stderr becomes the reason the model sees

Anything unexpected exits 2. A hook that cannot reach its supervisor must deny,
never wave the call through.

Standard library only: this runs once per tool call and must stay cheap.
"""

from __future__ import annotations

import json
import os
import socket
import sys

TIMEOUT_S = float(os.environ.get("DSA_HOOK_TIMEOUT", "150"))


def agent_id() -> str:
    """The agent this hook belongs to, from `--agent <id>` on its command line.

    Written into the per-agent hooks.json by the server, so the supervisor can
    resolve the workspace from its own registry rather than from the payload.
    """
    argv = sys.argv[1:]
    if "--agent" in argv:
        index = argv.index("--agent")
        if index + 1 < len(argv):
            return argv[index + 1]
    return ""


def block(reason: str) -> None:
    sys.stderr.write(reason.rstrip() + "\n")
    sys.exit(2)


def main() -> None:
    socket_path = os.environ.get("DSA_APPROVAL_SOCKET")
    if not socket_path:
        block("blocked: DSA_APPROVAL_SOCKET is not set, so no supervisor can be reached")

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError) as exc:
        block(f"blocked: hook could not read its input ({exc})")

    request = {
        # Claude Code's PreToolUse payload shape, which the dsh bridge emits.
        "tool_name": payload.get("tool_name") or payload.get("toolName") or "",
        "tool_input": payload.get("tool_input") or payload.get("toolInput") or {},
        "workspace": payload.get("cwd") or os.environ.get("DSH_CWD") or os.getcwd(),
        "agent_id": agent_id(),
    }

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT_S)
            sock.connect(socket_path)
            sock.sendall((json.dumps(request) + "\n").encode())
            chunks = []
            while b"\n" not in b"".join(chunks):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        reply = json.loads(b"".join(chunks).split(b"\n", 1)[0].decode())
    except (OSError, ValueError) as exc:
        block(f"blocked: supervisor unreachable ({type(exc).__name__}: {exc})")

    if reply.get("action") == "allow":
        sys.exit(0)
    block(reply.get("reason") or "blocked by the supervisor")


if __name__ == "__main__":
    main()
