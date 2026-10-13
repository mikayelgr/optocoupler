"""What a request needs: which task files (if any), and the result it must end with. Also the check that the result exists."""

from __future__ import annotations

import json
import re
import shutil

from rich import box
from rich.panel import Panel
from rich.table import Table

from . import llm
from .config import COLORS, RUN, TASK_DATA, TASK_DIR, console
from .prompts import DATA_NOTE, add_note
from .state import MESSAGE, RequestScope

SCOPE_PROMPT = """A user gave this request to a multi-agent team: an orchestrator, a scientist who writes plans, a
general agent who explains, and an executor who writes and runs Python scripts, and who can drive a light bench
(an LED and a lux meter) through tools.

Request:
{request}

{task}

Decide:
- `tasks`: the names of the task files the request asks the team to work on (or a part of), or refers to through
  something only that file defines, such as its homework or task number, its data or its model. Leave it empty for a
  request that stands on its own, even when it is on a related topic.{task_fixed}
- `result`: where the value the final answer reports must come from. `script` when the request asks to compute,
  measure or test something that only running code can give; `bench` when it asks to set or measure the light level
  on the bench (the LED, the light level or lux); `none` when it asks for an explanation,
  a plan, an answer from knowledge, or anything else without a measured value.
- `quantity`: for `script`, the short label a script would print next to that value (for example `RMSE`, `accuracy`,
  `mean`); for `bench`, `lux`; for `none`, an empty string.
- `reason`: one sentence."""



def read_tasks() -> dict[str, str]:
    """The text of every task file in task/, by file name."""
    import pymupdf

    tasks = {}
    for path in sorted(TASK_DIR.glob("*.pdf")):
        with pymupdf.open(str(path)) as doc:
            tasks[path.name] = "".join(page.get_text() for page in doc).strip()
    return tasks


def request_text(messages: list[MESSAGE]) -> str:
    # The user's request, the replies to any questions asked about it, and any steering notes, as one block.
    lines, question = [], ""
    for msg in messages:
        role = msg.get("role")
        if role == "user":
            lines.append(msg.get("text", ""))
        elif role == "orchestrator":
            question = json.loads(msg["text"]).get("question", "")
        elif role == "system":
            question = ""  # a human message after a cancelled step answers no question: it steers the work
        elif role == "human" and question:
            lines.append(f"Question asked about it: {question}\nUser's answer: {msg.get('text', '')}")
        elif role == "human":
            lines.append(f"User's note after cancelling a step: {msg.get('text', '')}")
    return "\n\n".join(lines)


def scope_request(request: str, tasks: dict[str, str], fixed_tasks: list[str] | None = None) -> RequestScope:
    """Decide which task files the request needs, and which measured result it must end with.

    `fixed_tasks` settles the task files beforehand (from --task, or an earlier scope); the call then only
    decides the result.
    """
    if tasks:
        task = "The project folder also holds these task files (homework sheets). Each one begins:\n\n" + "\n\n".join(
            f"--- {name} ---\n{text[:1500]}" for name, text in tasks.items()
        )
    else:
        task = "There are no task files in the project folder."
    prompt = SCOPE_PROMPT.format(
        request=request,
        task=task,
        task_fixed=f" It is already decided: answer {json.dumps(fixed_tasks)}." if fixed_tasks is not None else "",
    )
    scope = llm.decide(prompt, schema=RequestScope, status="Reading the request...")
    chosen = fixed_tasks if fixed_tasks is not None else scope.tasks
    updates = {"tasks": [name for name in chosen if name in tasks]}
    if scope.result == "script" and not scope.quantity.strip():
        updates["quantity"] = "result"
    return scope.model_copy(update=updates)


def drop_bench(scope: RequestScope) -> RequestScope:
    """A bench scope when no bench is connected: warn, and require no measured result."""
    console.print(
        Panel(
            "This request looks like a light-bench request, but the bench is not connected. Rerun with --bench.",
            title="[yellow]BENCH[/]",
            border_style="yellow",
        )
    )
    return scope.model_copy(update={"result": "none", "quantity": ""})


def show_scope(scope: RequestScope, title: str = "REQUEST SCOPE") -> None:
    table = Table(title=f"[{COLORS['orchestrator']}]{title}[/]", box=box.ROUNDED, border_style="cyan")
    table.add_column("Field", style="bold cyan")
    table.add_column("Value", style="white")
    table.add_row("Task files", ", ".join(scope.tasks) if scope.tasks else "none needed")
    table.add_row("Required result", scope.requirement)
    table.add_row("Reason", scope.reason)
    console.print(table)


def _is_mat_file(path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as f:
        return f.read(6) == b"MATLAB"


def provide_task_data(scope: RequestScope) -> None:
    """Copy the data that comes with the request's task files into the working directory."""
    for name in scope.tasks:
        data_file = TASK_DATA.get(name)
        if data_file is None:
            continue
        if not _is_mat_file(data_file):
            console.print(
                Panel(
                    f"{data_file.name} is missing or is not a MAT file. The executor will have to find the data with web search.",
                    title="[yellow]DATA[/]",
                    border_style="yellow",
                )
            )
            continue
        shutil.copy2(data_file, RUN.work_dir / data_file.name)
        add_note(DATA_NOTE)


def has_printed_value(messages: list[MESSAGE], quantity: str) -> bool:
    # The most recent executor message is a successful script run that printed the quantity with a number.
    for msg in reversed(messages):
        if msg.get("role") == "executor":
            run = re.search(
                r"^Exit code: 0\nStdout: (.*?)\nStderr:", msg.get("text", ""), re.M | re.S
            )
            return bool(run and re.search(rf"{re.escape(quantity)}\D{{0,40}}\d", run.group(1), re.I))
    return False


LUX_READING_RE = re.compile(r"read_lux\(\) -> \d+(\.\d+)? lux")


def has_bench_reading(messages: list[MESSAGE]) -> bool:
    # The most recent executor turn ended its bench work with a light reading.
    in_turn = False
    for msg in reversed(messages):
        role = msg.get("role")
        if role == "hardware":
            lines = msg.get("text", "").strip().splitlines()
            return bool(lines) and bool(LUX_READING_RE.fullmatch(lines[-1]))
        if role == "executor":
            in_turn = True
        elif in_turn:
            return False
    return False


def has_required_result(messages: list[MESSAGE], scope: RequestScope | None) -> bool:
    if scope is None or scope.result == "none":
        return True
    if scope.result == "bench":
        return has_bench_reading(messages)
    return has_printed_value(messages, scope.quantity)
