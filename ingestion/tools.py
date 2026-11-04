"""Outside programs run on outside files (PDFs from third parties), kept away from what this process holds.

A tool gets only the environment it needs, so a database URL, a sign-in secret or a mail key set for this
process does not reach it, and no open file or socket. It gets a time limit, and limits on memory, processor
time and the size of what it writes, so that one bad file fails rather than taking the instance with it.

The limits are set by a small interpreter that then becomes the tool (exec). `preexec_fn` would be the
direct way, but these processes run threads (a web server's, or a job's pool), and Python code between
fork and exec can deadlock the child when they do.

This is damage limitation, not a sandbox. A tool taken over by a crafted file still runs as this user with
this network: it can read /proc/<parent>/environ and the files this user can read, and ask the metadata
server for this service account's token. Only a process that holds no secrets, on no outbound network,
closes that.
"""

import os
import shutil
import subprocess
import sys

GIB = 1024**3
MIB = 1024**2
ENVIRONMENT = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR")
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
# Limits a running process may only lower. Where one cannot be set (macOS refuses an address-space limit
# below what it has already mapped), the tool still runs with the rest.
BECOME = """
import os, resource, sys
memory, cpu, written = (int(value) for value in sys.argv[1:4])
for limit, value, spare in (
    (resource.RLIMIT_CORE, 0, 0),
    (resource.RLIMIT_AS, memory, 0),
    (resource.RLIMIT_CPU, cpu, 5),
    (resource.RLIMIT_FSIZE, written, 0),
):
    try:
        resource.setrlimit(limit, (value, value + spare))
    except (OSError, ValueError):
        pass
os.execvp(sys.argv[4], sys.argv[4:])
"""


def minimal_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """What a tool needs and nothing else; `extra` adds a tool's own settings."""
    kept = {name: os.environ[name] for name in ENVIRONMENT if name in os.environ}
    kept.setdefault("PATH", DEFAULT_PATH)
    return {**kept, **(extra or {})}


def run_tool(
    command: list[str],
    *,
    timeout: float,
    memory: int | None = 3 * GIB,
    written: int = 256 * MIB,
    check: bool = False,
    extra_env: dict[str, str] | None = None,
    **options,
) -> subprocess.CompletedProcess:
    """`subprocess.run` for a tool reading an outside file: see the module's notes. Processor time is
    limited to the time limit, which a single-threaded tool cannot exceed. `memory=None` runs it with no
    limits at all but the time limit, for a tool that maps more than it uses (a model's weights)."""
    env = minimal_environment(extra_env)
    wrapped = command
    if memory is not None:
        # An interpreter that fails to become the tool is not the same as the tool not being there.
        if shutil.which(command[0], path=env["PATH"]) is None:
            raise FileNotFoundError(command[0])
        limits = (str(memory), str(int(timeout)), str(written))
        wrapped = [sys.executable, "-I", "-S", "-c", BECOME, *limits, *command]
    try:
        completed = subprocess.run(
            wrapped, env=env, timeout=timeout, stdin=subprocess.DEVNULL, check=False, **options
        )
    except subprocess.TimeoutExpired as expired:
        expired.cmd = command  # what ran, as it would be written
        raise
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, command, completed.stdout, completed.stderr)
    return completed
