"""Paths, limits and the settings of the current run."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

if TYPE_CHECKING:
    from .bench import Bench

console = Console()

PROJECT_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_DIR / "prompts"
TASK_DIR = PROJECT_DIR / "task"  # every *.pdf here is a task file a request can refer to
DATA_FILE = TASK_DIR / "SNLS80mV.mat"
TASK_DATA = {"task1.pdf": DATA_FILE}  # data that comes with a task file, copied in when the request uses it
BENCH_SERVER = PROJECT_DIR / "bench" / "server.py"
BENCH_HELPER = PROJECT_DIR / "bench" / "bench_tools.py"  # copied into the working directory for scripts

MODEL = "google/gemma-4-31b-it"  # every role's model, served through OpenRouter

MAX_DECISIONS = 0  # 0 means no limit (the default); Homework 1, Task 6 uses 8. Set with --max-decisions.
WEB_SEARCH_ROUNDS = 3  # search rounds the executor may use per turn
FINISH_REJECTIONS = 2  # times the orchestrator is re-asked after an unjustified finish
HARDWARE_ROUNDS = 12  # bench calls (hardware turns) the executor may make per turn
ORCHESTRATOR_MAX_CONTEXT = 200_000

COLORS = {
    "orchestrator": "bold cyan",
    "scientist": "bold magenta",
    "general": "bold green",
    "executor": "bold yellow",
    "summarizer": "bold blue",
    "web search": "bold yellow",
    "hardware": "bold bright_red",
    "human": "bold red",
    "system": "dim",
}


@dataclass
class Run:
    """Settings of the current run, set once at startup by the CLI."""

    work_dir: Path = Path(".")  # --workdir, or a fresh temp directory
    max_decisions: int = MAX_DECISIONS
    bench: Bench | None = None  # set with --bench
    tasks: dict[str, str] = field(default_factory=dict)  # task file name -> its text


RUN = Run()
