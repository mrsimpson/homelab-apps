"""
sandbox-mcp: A minimal MCP server exposing sandboxed code execution tools.

Wraps sandlock (Landlock + seccomp) for per-call process isolation.
Serves Streamable HTTP MCP at POST /mcp on port 8888.

sandlock's Sandbox.run() fails when called from within the uvicorn server
process (PID 1) under Kubernetes RuntimeDefault seccomp. The root cause is
still under investigation (see multikernel/sandlock#47). As a workaround,
each sandbox invocation is dispatched to a fresh single-threaded helper
subprocess where sandlock works correctly.
"""

import asyncio
import logging
import pathlib
import tempfile
from mcp.server.fastmcp import FastMCP
from sandlock import Sandbox, landlock_abi_version, LandlockUnavailableError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sandbox_mcp")

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
# Startup diagnostic: call Sandbox.run() directly from this process to
# surface the exact do_create error via the new Rust eprintln! logging.
# The result is discarded — the subprocess workaround handles real calls.
# ---------------------------------------------------------------------------
def _diag_direct_sandlock():
    import os, threading
    ws = SESSION_BASE / "_diag_"
    ws.mkdir(parents=True, exist_ok=True)
    # Capture stderr so the Rust eprintln! output lands in our log
    r_fd, w_fd = os.pipe()
    old_stderr = os.dup(2)
    os.dup2(w_fd, 2)
    os.close(w_fd)
    try:
        sb = Sandbox(
            fs_readable=["/usr", "/lib", "/etc"],
            fs_writable=[str(ws)],
            net_allow=[],
            max_memory="256M",
            max_processes=20,
            clean_env=True,
            env={"HOME": str(ws), "TMPDIR": str(ws), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
        result = sb.run(["python3", "-c", "print(1)"], timeout=5.0)
        diag_ok = result.success
    except Exception as exc:
        diag_ok = False
    finally:
        os.dup2(old_stderr, 2)
        os.close(old_stderr)
        os.set_blocking(r_fd, False)
        try:
            rust_stderr = os.read(r_fd, 8192).decode(errors="replace").strip()
        except BlockingIOError:
            rust_stderr = ""
        os.close(r_fd)
    log.info("diag direct sandlock: pid=%d threads=%d ok=%s rust_stderr=%r",
             os.getpid(), threading.active_count(), diag_ok, rust_stderr)

_diag_direct_sandlock()

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


def _run_sandboxed_sync(cmd: list[str], ws: pathlib.Path, timeout: int = 30) -> str:
    """Execute cmd inside a sandlock sandbox via an isolated helper subprocess.

    Sandlock's Sandbox.run() fails when called from the uvicorn server process
    (PID 1, multi-threaded, under Kubernetes RuntimeDefault seccomp). The
    workaround: spawn a fresh single-threaded Python helper that calls sandlock
    and returns the result over a pipe.
    """
    import json as _json
    import subprocess as _sp
    import sys as _sys

    helper = r"""
import sys, json, pathlib
from sandlock import Sandbox, LandlockUnavailableError

req = json.loads(sys.stdin.read())
ws = pathlib.Path(req["ws"])
cmd = req["cmd"]
timeout = req["timeout"]

readable = ["/usr", "/lib", "/etc"]
lib64 = pathlib.Path("/lib64")
if lib64.exists() and not lib64.is_symlink():
    readable.append("/lib64")

sb = Sandbox(
    fs_readable=readable,
    fs_writable=[str(ws)],
    net_allow=[],
    max_memory="256M",
    max_processes=20,
    clean_env=True,
    env={"HOME": str(ws), "TMPDIR": str(ws), "PATH": "/usr/local/bin:/usr/bin:/bin"},
)

try:
    result = sb.run(cmd, timeout=float(timeout))
    stdout = result.stdout.decode(errors="replace")
    stderr = result.stderr.decode(errors="replace")
    output = stdout
    if stderr:
        output = output + ("\n" if output else "") + stderr
    if not result.success:
        err = getattr(result, "error", None)
        prefix = f"[exit {result.exit_code}]"
        if err:
            prefix += f" {err}"
        output = prefix + ("\n" + output if output else "")
    print(json.dumps({"ok": True, "output": output or "(no output)"}))
except LandlockUnavailableError as e:
    print(json.dumps({"ok": False, "output": f"[error] Landlock unavailable: {e}"}))
except Exception as e:
    print(json.dumps({"ok": False, "output": f"[error] {e}"}))
"""

    request = _json.dumps({"cmd": cmd, "ws": str(ws), "timeout": timeout})
    log.info("spawn (via helper): cmd=%s ws=%s", cmd, ws)
    try:
        proc = _sp.run(
            [_sys.executable, "-c", helper],
            input=request,
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        if proc.returncode != 0 and not proc.stdout.strip():
            log.error("helper stderr: %s", proc.stderr[:500])
            return f"[error] helper exited {proc.returncode}: {proc.stderr[:200]}"
        resp = _json.loads(proc.stdout.strip())
        result_str = resp.get("output", "(no output)")
        log.info("done: ok=%s output=%r", resp.get("ok"), result_str[:80])
        return result_str
    except _sp.TimeoutExpired:
        log.error("helper timed out after %ss", timeout + 5)
        return f"[error] execution timed out after {timeout}s"
    except Exception as exc:
        log.exception("helper invocation failed: cmd=%s", cmd)
        return f"[error] {exc}"


async def _run_sandboxed(cmd: list[str], ws: pathlib.Path, timeout: int = 30) -> str:
    """Async wrapper: offloads blocking _run_sandboxed_sync to a thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _run_sandboxed_sync, cmd, ws, timeout
    )


# ---------------------------------------------------------------------------
# Tools — all async so FastMCP dispatches them correctly and we can
# await run_in_executor for the blocking subprocess call.
# ---------------------------------------------------------------------------

@mcp.tool()
async def execute_python(code: str, session_id: str = "default") -> str:
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
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir=ws, delete=False, prefix="_exec_"
    ) as f:
        f.write(code)
        script_path = f.name
    try:
        return await _run_sandboxed(["python3", script_path], ws)
    finally:
        try:
            pathlib.Path(script_path).unlink()
        except OSError:
            pass


@mcp.tool()
async def run_shell(command: str, session_id: str = "default") -> str:
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
    return await _run_sandboxed(["sh", "-c", command], ws)


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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    app = mcp.streamable_http_app()
    uvicorn.run(app, host="0.0.0.0", port=8888)
