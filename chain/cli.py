"""Command line: parse options, scope the request, run the graph, write the transcript and report."""

from __future__ import annotations

import argparse
import os
import tempfile
import time
import traceback
import uuid
from pathlib import Path

from langgraph.errors import GraphRecursionError
from rich.panel import Panel
from rich.text import Text

from .bench import start_bench
from .config import COLORS, MAX_DECISIONS, RUN, console
from .graph import build_graph, run_thread
from .prompts import TASK_NOTE, add_note
from .report import write_report, write_transcript
from .scope import has_required_result, provide_task_data, read_tasks, scope_request, show_scope
from .state import DialogueState, decision_count, load_scope

BANNER = r"""
╔═════════════════════════════════════════════════════════╗
║            Photonics AI Solver Chain                    ║
║          multi-agent research pipeline                  ║
╚═════════════════════════════════════════════════════════╝
""".strip()


def parse_args() -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(prog="python .", description="Photonics AI Solver Chain")
    parser.add_argument(
        "--request",
        help="your request to the team, for scripted runs. Without it (and without --task), you type the "
        "request at a prompt. Task files from task/ are added as background only if the request refers to them",
    )
    parser.add_argument(
        "--task",
        action="append",
        metavar="NAME",
        help="a task file from task/ to work on, e.g. task1 or task3.pdf (repeat for several), or `none`. "
        "With --request it is background to the request; without one, the task file is the request. "
        "Default: decided from the request",
    )
    parser.add_argument(
        "--max-decisions",
        type=int,
        default=MAX_DECISIONS,
        help="orchestrator decision limit; 0 (the default) means no limit. Homework 1, Task 6 uses 8",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        help="working directory to use (and keep results in) across runs; default: a new temp directory",
    )
    parser.add_argument(
        "--bench",
        action="store_true",
        help="connect the light bench MCP server (bench/server.py) and give its tools to the executor",
    )
    parser.add_argument(
        "--pico-port",
        default=os.environ.get("PICO_PORT"),
        help="serial port of the Pico for --bench, e.g. /dev/cu.usbmodem31201 or /dev/ttyACM0 "
        "(default: $PICO_PORT, else the connected Pico is found automatically)",
    )
    parser.add_argument(
        "--phyphox-url",
        default=os.environ.get("PHYPHOX_URL"),
        help="phyphox address on the phone for --bench (default: $PHYPHOX_URL, else http://localhost:8080)",
    )
    parser.add_argument(
        "--thread",
        default=None,
        help="LangGraph thread id (default: a new one per run)",
    )
    return parser, parser.parse_args()


def resolve_tasks(parser: argparse.ArgumentParser, names: list[str] | None) -> list[str] | None:
    """--task values as task file names; [] for `none`; None when the request should decide."""
    if not names:
        return None
    if "none" in names:
        if len(names) > 1:
            parser.error("--task none cannot be combined with other task files")
        return []
    chosen = []
    for name in names:
        match = next((n for n in (name, f"{name}.pdf") if n in RUN.tasks), None)
        if match is None:
            parser.error(f"no task file {name!r} in task/ (there are: {', '.join(RUN.tasks) or 'none'})")
        chosen.append(match)
    return chosen


def _task_text(name: str) -> str:
    return f"{name}:\n\n{RUN.tasks[name]}"


def initial_messages(request: str | None, tasks: list[str]) -> list[dict]:
    if request is None:
        # No --request: the task files themselves are the request.
        return [{"role": "user", "text": "\n\n".join(_task_text(name) for name in tasks)}]
    if tasks:
        # The task files are background to the request, each in a message of its own.
        add_note(TASK_NOTE)
        return [{"role": "task", "text": _task_text(name)} for name in tasks] + [{"role": "user", "text": request}]
    return [{"role": "user", "text": request}]


def ask_request() -> str:
    """Ask for the request in the terminal; Ctrl+C or end of input raises."""
    body = Text("What should the team work on?\n", style="bold white")
    if RUN.tasks:
        body.append(
            f"\nTask files in task/: {', '.join(RUN.tasks)}. Mention one (for example \"Homework 1, Task 5\") "
            "and the team gets that sheet.",
            style="dim",
        )
    if RUN.bench:
        body.append(f"\nLight bench connected: {', '.join(RUN.bench.names)}.", style="dim")
    console.print(
        Panel(
            body,
            title=f"[{COLORS['orchestrator']}]YOUR REQUEST[/]",
            subtitle="[dim]Enter sends · Ctrl+C quits[/]",
            border_style="cyan",
        )
    )
    while True:
        request = console.input(f"[{COLORS['orchestrator']}]request › [/]").strip()
        if request:
            return request


def offer_bench(pico_port: str | None, phyphox_url: str | None) -> bool:
    """A bench request without --bench: offer to connect the bench now. False means nothing should run."""
    console.print(
        Panel(
            "This is a light-bench request, but the bench isn't connected (no --bench). Without it the team has no "
            "way to reach the LED or the light sensor.",
            title="[yellow]BENCH NOT CONNECTED[/]",
            border_style="yellow",
        )
    )
    try:
        answer = console.input(f"[{COLORS['orchestrator']}]Connect the light bench now? \\[Y/n] › [/]").strip().lower()
    except (KeyboardInterrupt, EOFError):
        answer = "n"
    if answer in ("", "y", "yes"):
        RUN.bench = start_bench(pico_port, phyphox_url)
        return True
    console.print("[dim]Nothing ran. Start with --bench to give the executor the bench tools.[/]")
    return False


def run_graph(initial_state: DialogueState, thread_id: str) -> list[dict]:
    """Run the graph to the end; on any stop, return the dialogue so far."""
    compiled = build_graph()
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 10_000}
    try:
        run_thread(compiled, initial_state, config)
        values = compiled.get_state(config).values
        if (
            RUN.max_decisions
            and decision_count(values["messages"]) >= RUN.max_decisions
            and not has_required_result(values["messages"], load_scope(values.get("scope")))
        ):
            console.print(
                Panel(
                    f"Decision limit ({RUN.max_decisions}) reached without the required result. "
                    "Summarizing the work so far; rerun without --max-decisions (or with a higher one) to keep going until done.",
                    title="[yellow]DECISION LIMIT[/]",
                    border_style="yellow",
                )
            )
    except (KeyboardInterrupt, EOFError):
        console.print(
            Panel("Interrupted. Summarizing the work so far.", title="[yellow]INTERRUPTED[/]", border_style="yellow")
        )
    except GraphRecursionError:
        console.print(
            Panel(
                "LangGraph step limit reached. Summarizing the work so far.",
                title="[yellow]STEP LIMIT[/]",
                border_style="yellow",
            )
        )
    except Exception:
        console.print(Panel(traceback.format_exc(), title="[red]GRAPH HALTED[/]", border_style="red"))
    return compiled.get_state(config).values.get("messages") or initial_state.messages


def main() -> None:
    parser, args = parse_args()
    RUN.max_decisions = args.max_decisions
    RUN.tasks = read_tasks()
    fixed_tasks = resolve_tasks(parser, args.task)
    user_request = args.request

    if args.workdir:
        RUN.work_dir = args.workdir.resolve()
        RUN.work_dir.mkdir(parents=True, exist_ok=True)
    else:
        RUN.work_dir = Path(tempfile.mkdtemp(prefix="photonics_"))
    thread_id = args.thread or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    console.print(Panel(BANNER, style="bold cyan", border_style="cyan"))
    console.print(f"[dim]Thread: {thread_id}   Working directory: {RUN.work_dir}[/]")
    if args.bench:
        RUN.bench = start_bench(args.pico_port, args.phyphox_url)
    try:
        if user_request is None and not fixed_tasks:
            try:
                user_request = ask_request()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]No request given, so nothing ran.[/]")
                return
        # Without a request, the task files named with --task are the request.
        request = user_request or "\n\n".join(_task_text(name) for name in fixed_tasks)
        scope = scope_request(request, RUN.tasks, fixed_tasks)
        show_scope(scope)
        if scope.result == "bench" and not RUN.bench and not offer_bench(args.pico_port, args.phyphox_url):
            return
        messages = initial_messages(user_request, scope.tasks)
        provide_task_data(scope)
        final_messages = run_graph(DialogueState(messages=messages, scope=scope.model_dump()), thread_id)
    finally:
        if RUN.bench:
            RUN.bench.close()

    transcript_path = write_transcript(final_messages, thread_id)
    console.print(f"[dim]Dialogue transcript saved to {transcript_path}[/]")
    write_report(final_messages)
