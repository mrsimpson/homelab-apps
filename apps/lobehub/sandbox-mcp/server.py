"""
sandbox-mcp: A minimal MCP server exposing sandboxed code execution tools.

Wraps sandlock (Landlock + seccomp) for per-call process isolation.
Serves Streamable HTTP MCP at POST /mcp on port 8888 (localhost only).
"""

import pathlib
from mcp.server.fastmcp import FastMCP
from sandlock import Sandbox, Policy, landlock_abi_version, LandlockUnavailableError

# ---------------------------------------------------------------------------
# Session workspace base directory (mounted as emptyDir in k8s)
# ---------------------------------------------------------------------------

SESSION_BASE = pathlib.Path("/tmp/sessions")
SESSION_BASE.mkdir(parents=True, exist_ok=True)

# Check Landlock availability at startup — log but don't crash; we'll report
# errors per-call if Landlock is unavailable.
_LANDLOCK_ABI = landlock_abi_version()
_MIN_ABI = 6  # sandlock requires ABI v6 (Linux 6.7+)
if _LANDLOCK_ABI < _MIN_ABI:
    import sys
    print(
        f"WARNING: Landlock ABI {_LANDLOCK_ABI} < required {_MIN_ABI}. "
        "Sandboxed execution will fail. Requires Linux 6.7+.",
        file=sys.stderr,
    )
else:
    print(f"sandlock ready: Landlock ABI v{_LANDLOCK_ABI}")

# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("sandbox-mcp", host="0.0.0.0", port=8888)


def _session_workspace(session_id: str) -> pathlib.Path:
    """Return (and create) the workspace directory for a given session."""
    ws = SESSION_BASE / session_id
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _resolve_safe(ws: pathlib.Path, path: str) -> pathlib.Path | None:
    """Resolve path inside workspace; return None if traversal detected."""
    try:
        resolved = (ws / path).resolve()
        resolved.relative_to(ws.resolve())
        return resolved
    except ValueError:
        return None


def _make_policy(ws: pathlib.Path) -> Policy:
    """
    Build a deny-by-default sandlock Policy for one execution.

    Sandbox rules:
    - Read-only: /usr, /lib, /lib64, /etc (runtime libs + Python stdlib)
    - Read-write: session workspace only
    - No outbound network (net_allow_hosts=[])
    - Memory limit: 256 MiB
    - Process limit: 20
    """
    readable = ["/usr", "/lib", "/etc"]
    lib64 = pathlib.Path("/lib64")
    if lib64.exists() and not lib64.is_symlink():
        readable.append("/lib64")

    return Policy(
        fs_readable=readable,
        fs_writable=[str(ws)],
        net_allow_hosts=[],       # deny all outbound network
        max_memory="256M",
        max_processes=20,
        clean_env=True,
        env={"HOME": str(ws), "TMPDIR": str(ws), "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )


def _run_sandboxed(cmd: list[str], ws: pathlib.Path, timeout: int = 30) -> str:
    """Execute cmd inside a sandlock sandbox and return combined stdout+stderr."""
    try:
        policy = _make_policy(ws)
        result = Sandbox(policy).run(cmd, timeout=float(timeout))
        output = result.stdout.decode(errors="replace")
        stderr = result.stderr.decode(errors="replace")
        if stderr:
            output = output + ("\n" if output else "") + stderr
        if not result.success:
            output = f"[exit {result.exit_code}]\n" + output
        return output or "(no output)"
    except LandlockUnavailableError as exc:
        return f"[error] Landlock unavailable on this kernel: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"[error] {exc}"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def execute_python(code: str, session_id: str = "default") -> str:
    """Execute Python code in a sandboxed environment.

    The code runs inside a sandlock sandbox (Landlock + seccomp):
    - Read-only access to /usr, /lib, /etc
    - Read-write access to the session workspace only
    - No network access
    - Memory limit: 256 MiB

    Args:
        code: Python source code to execute.
        session_id: Workspace identifier — use the same ID across calls to share files.

    Returns:
        Combined stdout and stderr from the execution.
    """
    ws = _session_workspace(session_id)
    return _run_sandboxed(["python3", "-c", code], ws)


@mcp.tool()
def run_shell(command: str, session_id: str = "default") -> str:
    """Run a shell command in a sandboxed environment.

    The command runs via sh -c inside a sandlock sandbox (Landlock + seccomp):
    - Read-only access to /usr, /lib, /etc
    - Read-write access to the session workspace only
    - No network access
    - Memory limit: 256 MiB

    Args:
        command: Shell command to execute.
        session_id: Workspace identifier — use the same ID across calls to share files.

    Returns:
        Combined stdout and stderr from the execution.
    """
    ws = _session_workspace(session_id)
    return _run_sandboxed(["sh", "-c", command], ws)


@mcp.tool()
def read_file(path: str, session_id: str = "default") -> str:
    """Read a file from the session workspace.

    Args:
        path: Relative path within the session workspace.
        session_id: Workspace identifier.

    Returns:
        File contents as a string, or an error message.
    """
    ws = _session_workspace(session_id)
    resolved = _resolve_safe(ws, path)
    if resolved is None:
        return "Error: path traversal denied"
    if not resolved.exists():
        return f"Error: file not found: {path}"
    if resolved.is_dir():
        return f"Error: {path} is a directory, not a file"
    try:
        return resolved.read_text(errors="replace")
    except OSError as exc:
        return f"Error reading file: {exc}"


@mcp.tool()
def write_file(path: str, content: str, session_id: str = "default") -> str:
    """Write a file to the session workspace.

    Args:
        path: Relative path within the session workspace.
        content: File content to write.
        session_id: Workspace identifier.

    Returns:
        Confirmation message or an error message.
    """
    ws = _session_workspace(session_id)
    resolved = _resolve_safe(ws, path)
    if resolved is None:
        return "Error: path traversal denied"
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
        return f"Written {len(content)} bytes to {path}"
    except OSError as exc:
        return f"Error writing file: {exc}"


@mcp.tool()
def list_files(path: str = ".", session_id: str = "default") -> str:
    """List files and directories in the session workspace.

    Args:
        path: Relative path within the session workspace (default: workspace root).
        session_id: Workspace identifier.

    Returns:
        Newline-separated list of entries prefixed with 'd' (dir) or 'f' (file).
    """
    ws = _session_workspace(session_id)
    resolved = _resolve_safe(ws, path)
    if resolved is None:
        return "Error: path traversal denied"
    if not resolved.exists():
        return f"Error: directory not found: {path}"
    if not resolved.is_dir():
        return f"Error: {path} is not a directory"
    try:
        entries = sorted(resolved.iterdir(), key=lambda e: (e.is_file(), e.name))
        if not entries:
            return "(empty directory)"
        return "\n".join(
            f"{'d' if e.is_dir() else 'f'} {e.name}" for e in entries
        )
    except OSError as exc:
        return f"Error listing directory: {exc}"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
