"""
sandbox-mcp: A minimal MCP server exposing sandboxed code execution tools.

Wraps sandlock (Landlock + seccomp) for per-call process isolation.
Serves Streamable HTTP MCP at POST /mcp on port 8888 (localhost only).

Important: all tools that call Sandbox.run() are declared async and
offload the blocking native call to a thread pool via run_in_executor.
FastMCP calls sync tools directly on the event loop thread, which causes
sandlock's fork()-based supervisor to fail (sandlock_spawn returns null
when called from within a running asyncio event loop). Making tools async
and using run_in_executor matches the pattern in sandlock's own MCP server.
"""

import asyncio
import logging
import pathlib
import tempfile
from mcp.server.fastmcp import FastMCP
from sandlock import Sandbox, Policy, landlock_abi_version, LandlockUnavailableError

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


def _run_sandboxed_sync(cmd: list[str], ws: pathlib.Path, timeout: int = 30) -> str:
    """Execute cmd inside a sandlock sandbox via an isolated helper subprocess.

    Sandlock's native fork()-based supervisor fails when called from a
    multi-threaded Python process (uvicorn). The workaround: spawn a
    fresh single-threaded Python helper that calls sandlock and returns
    the result over a pipe. The helper is isolated from the event loop.
    """
    import json as _json
    import subprocess as _sp
    import sys as _sys

    # The helper script: receives policy+cmd as JSON on stdin, runs sandlock,
    # returns {"ok": bool, "output": str, "error": str|null} as JSON on stdout.
    helper = r"""
import sys, json, pathlib
from sandlock import Sandbox, Policy, LandlockUnavailableError

req = json.loads(sys.stdin.read())
ws = pathlib.Path(req["ws"])
cmd = req["cmd"]
timeout = req["timeout"]

readable = ["/usr", "/lib", "/etc"]
lib64 = pathlib.Path("/lib64")
if lib64.exists() and not lib64.is_symlink():
    readable.append("/lib64")

policy = Policy(
    fs_readable=readable,
    fs_writable=[str(ws)],
    net_allow_hosts=[],
    max_memory="256M",
    max_processes=20,
    clean_env=True,
    env={"HOME": str(ws), "TMPDIR": str(ws), "PATH": "/usr/local/bin:/usr/bin:/bin"},
)

try:
    result = Sandbox(policy).run(cmd, timeout=float(timeout))
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
# await run_in_executor for the blocking Sandbox.run() call.
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
    # Write code to a temp file in the workspace (avoids ARG_MAX limits with
    # long scripts; the root filesystem is read-only — only /tmp/sessions is
    # writable via emptyDir).
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
# Debug middleware + spawn test endpoint
#
# Logs the raw JSON body of every incoming MCP POST (truncated to 2 KB so
# we can see exactly what LobeHub sends without drowning the logs).
# Also exposes GET /debug/spawn — curl it from inside the pod to trigger
# a sandlock spawn from within the running server process and see if it works.
# ---------------------------------------------------------------------------

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from starlette.routing import Route


class _LogBodyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        if request.method == "POST" and request.url.path == "/mcp":
            try:
                body = await request.body()
                log.info("MCP POST body (%d bytes): %s", len(body), body[:2048].decode(errors="replace"))
            except Exception:
                pass
        return await call_next(request)


def _debug_spawn_sync() -> dict:
    """Run sandlock spawn synchronously — call from run_in_executor."""
    import os, ctypes, ctypes.util, threading
    from sandlock import Sandbox, Policy
    from sandlock._sdk import _lib, _NativePolicy, _make_argv

    info = {
        "pid": os.getpid(),
        "tid": threading.get_ident(),
        "active_threads": threading.active_count(),
    }

    # Read seccomp filter count for this thread
    try:
        with open(f"/proc/self/status") as f:
            for line in f:
                if "Seccomp" in line:
                    info["seccomp"] = line.strip()
    except Exception as e:
        info["seccomp_err"] = str(e)

    ws = _session_workspace("__debug__")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", dir=ws, delete=False, prefix="_dbg_") as f:
        f.write("print('debug spawn ok')\n")
        sp = f.name

    try:
        # Test 1: minimal policy (no net restriction)
        p_min = Policy(
            fs_readable=["/usr", "/lib", "/etc"],
            fs_writable=[str(ws)],
            clean_env=True,
            env={"HOME": str(ws), "TMPDIR": str(ws), "PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
        r_min = Sandbox(p_min).run(["python3", sp], timeout=5.0)
        info["minimal_policy"] = {"ok": r_min.success, "error": getattr(r_min, "error", None)}

        # Test 2: full policy as used by tools
        p_full = _make_policy(ws)
        r_full = Sandbox(p_full).run(["python3", sp], timeout=5.0)
        info["full_policy"] = {"ok": r_full.success, "error": getattr(r_full, "error", None)}

        # Test 3: raw fork()
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        info["fork"] = "ok"

        # Test 4: epoll_create1 (used by Tokio runtime)
        import ctypes.util as _cu
        _libc = ctypes.CDLL(_cu.find_library("c"), use_errno=True)
        efd = _libc.epoll_create1(0)
        errno_ = ctypes.get_errno()
        info["epoll_create1"] = f"fd={efd} errno={errno_}"
        if efd >= 0:
            _libc.close(efd)

        # Test 5: clone with CLONE_THREAD (what Tokio uses for worker threads)
        CLONE_THREAD = 0x00010000
        CLONE_VM     = 0x00000100
        CLONE_SIGHAND= 0x00000800
        CLONE_FS     = 0x00000200
        CLONE_FILES  = 0x00000400
        CLONE_SYSVSEM= 0x00040000
        CLONE_SETTLS = 0x00080000
        CLONE_PARENT_SETTID = 0x00100000
        CLONE_CHILD_CLEARTID = 0x00200000
        # Don't actually try clone(CLONE_THREAD) - it's too complex without proper TLS setup
        # Instead check if CLONE_NEWUSER is available (sandlock may try this)
        CLONE_NEWUSER = 0x10000000
        ret = _libc.unshare(CLONE_NEWUSER)
        errno_ = ctypes.get_errno()
        info["unshare_newuser"] = f"ret={ret} errno={errno_}"

        # Test 6: check /proc/sys/user/max_user_namespaces
        try:
            max_ns = pathlib.Path("/proc/sys/user/max_user_namespaces").read_text().strip()
            info["max_user_namespaces"] = max_ns
        except Exception as e:
            info["max_user_namespaces"] = f"error: {e}"

        # Test 7: check if prctl(PR_SET_SECCOMP) is blocked
        PR_SET_SECCOMP = 22
        SECCOMP_MODE_STRICT = 1
        # Don't actually set strict mode, just test another prctl
        PR_GET_SECCOMP = 21
        ret = _libc.prctl(PR_GET_SECCOMP, 0, 0, 0, 0)
        errno_ = ctypes.get_errno()
        info["prctl_get_seccomp"] = f"ret={ret} errno={errno_}"

        # Test 8: fork + check child's /proc/self/fd count
        pipe_r, pipe_w = os.pipe()
        pid2 = os.fork()
        if pid2 == 0:
            os.close(pipe_r)
            import pathlib as _pl
            fds = list(_pl.Path('/proc/self/fd').iterdir())
            msg = f"child fds={len(fds)}".encode()
            os.write(pipe_w, msg)
            os.close(pipe_w)
            os._exit(0)
        os.close(pipe_w)
        child_info = os.read(pipe_r, 256).decode()
        os.close(pipe_r)
        os.waitpid(pid2, 0)
        info["fork_child_fd_count"] = child_info

        # Test 9: clone3 availability (what Tokio uses for thread spawning)
        SYS_clone3 = 435
        SIGCHLD = 17
        class _ca(ctypes.Structure):
            _fields_ = [('flags',ctypes.c_uint64),('pidfd',ctypes.c_uint64),('child_tid',ctypes.c_uint64),('parent_tid',ctypes.c_uint64),('exit_signal',ctypes.c_uint64),('stack',ctypes.c_uint64),('stack_size',ctypes.c_uint64),('tls',ctypes.c_uint64),('set_tid',ctypes.c_uint64),('set_tid_size',ctypes.c_uint64),('cgroup',ctypes.c_uint64)]
        _args = _ca(); _args.flags = 0; _args.exit_signal = SIGCHLD
        ret3 = _libc.syscall(SYS_clone3, ctypes.byref(_args), ctypes.sizeof(_args))
        _e3 = ctypes.get_errno()
        if ret3 == 0: os._exit(0)
        elif ret3 > 0: os.waitpid(ret3, 0)
        info["clone3"] = f"ret={ret3} errno={_e3} ({os.strerror(_e3)})"

        # Test 10: try spawning a real OS thread via ctypes (like Tokio does)
        import threading as _thr
        _thr_result = []
        def _t():
            _thr_result.append(threading.get_ident())
        _tt = _thr.Thread(target=_t)
        _tt.start(); _tt.join(timeout=5)
        info["new_thread"] = f"ok tid={_thr_result[0]}" if _thr_result else "TIMEOUT"

    except Exception as exc:
        info["exception"] = str(exc)
    finally:
        pathlib.Path(sp).unlink(missing_ok=True)

    return info


async def _debug_spawn(request: StarletteRequest):
    """Trigger sandlock spawn tests from within the server process."""
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _debug_spawn_sync)
    log.info("debug_spawn result: %s", info)
    return JSONResponse(info)


# ---------------------------------------------------------------------------
# Entry point — mount middleware and debug route onto the FastMCP ASGI app
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette

    # Get the underlying ASGI app from FastMCP and wrap it
    _fastmcp_app = mcp.streamable_http_app()
    _app = Starlette(
        routes=[
            Route("/debug/spawn", _debug_spawn, methods=["GET"]),
        ],
    )

    # Chain: debug routes first, then MCP app for everything else
    from starlette.middleware.base import BaseHTTPMiddleware as _BM

    class _Router:
        def __init__(self):
            self._debug = _app
            self._mcp = _fastmcp_app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"].startswith("/debug/"):
                await self._debug(scope, receive, send)
            else:
                await self._mcp(scope, receive, send)

    _router = _Router()
    _wrapped = _LogBodyMiddleware(_router)

    uvicorn.run(_wrapped, host="0.0.0.0", port=8888)
