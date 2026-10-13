"""The executor's tools: run a script, install packages, search the web. All work in the run's working directory."""

from __future__ import annotations

import itertools
import os
import re
import signal
import subprocess
import sys

from . import llm
from .config import PROJECT_DIR, RUN

_script_counter = itertools.count(1)


def code_tool(code: str, timeout: int = 300) -> tuple[int, str, str]:
    # Skip numbers already used, so a reused --workdir never overwrites an earlier script.
    file_path = RUN.work_dir / f"script_{next(_script_counter):03d}.py"
    while file_path.exists():
        file_path = RUN.work_dir / f"script_{next(_script_counter):03d}.py"
    file_path.write_text(code, encoding="utf-8")
    # With --bench, the script gets the bridge address and token that bench_tools.py reads.
    env = {**os.environ, **RUN.bench.script_env} if RUN.bench else None
    # The script runs on the pipeline's own Python, so it sees the same packages (a uv or a pip environment). It gets
    # a process group of its own, so a timeout or Ctrl+C also stops any processes it started.
    with subprocess.Popen(
        [sys.executable, str(file_path)],
        cwd=RUN.work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException as exc:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, _ = process.communicate()
            if isinstance(exc, subprocess.TimeoutExpired):
                return -1, stdout, f"Timed out after {timeout}s"
            raise  # Ctrl+C: the step is cancelled
    return process.returncode, stdout, stderr


REQUIREMENT_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?"
    r"([<>=!~]=?[A-Za-z0-9.*+!_-]+(,[<>=!~]=?[A-Za-z0-9.*+!_-]+)*)?"
)


def dependency_tool(packages: list[str], timeout: int = 600) -> tuple[int, str, str]:
    invalid = [pkg for pkg in packages if not REQUIREMENT_RE.fullmatch(pkg)]
    if invalid:
        return -1, "", f"Rejected invalid package spec(s): {', '.join(invalid)}"
    # Started with `uv run` (which sets UV): `uv add` records the packages in pyproject.toml and uv.lock.
    # Otherwise pip installs them into the environment the pipeline runs in.
    uv = os.environ.get("UV")
    command = [uv, "add", "--project", str(PROJECT_DIR)] if uv else [sys.executable, "-m", "pip", "install"]
    try:
        result = subprocess.run([*command, *packages], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "", f"Installing packages timed out after {timeout}s"
    return result.returncode, result.stdout, result.stderr


def web_search_tool(query: str) -> str:
    model = llm.chat_model(plugins=[{"id": "web", "max_results": 5}])
    message = (
        f"{query}\n\nAnswer from the web search results. List every relevant source as "
        "`URL - what it contains`, and quote direct file download links exactly."
    )
    reply, _ = llm.stream_with_retry(model, message, "WEB SEARCH")
    return reply


def workdir_listing(max_entries: int = 50) -> str:
    entries = sorted(
        path for path in RUN.work_dir.rglob("*") if not path.name.startswith("script_")
    )
    lines = [
        f"- {path.relative_to(RUN.work_dir)}{'/' if path.is_dir() else f' ({path.stat().st_size} bytes)'}"
        for path in entries[:max_entries]
    ]
    if len(entries) > max_entries:
        lines.append(f"- ... and {len(entries) - max_entries} more")
    listing = "\n".join(lines) or "(empty: no data has been downloaded yet)"
    return f"\nWorking directory for this run: {RUN.work_dir}\nFiles in it (scripts excluded):\n{listing}\n"
