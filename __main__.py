from __future__ import annotations

import argparse
import itertools
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, StateGraph
from langgraph.types import Command, interrupt
from langchain_openrouter import ChatOpenRouter
from rich.console import Console
from rich.panel import Panel
from rich.live import Live
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

PROJECT_DIR = Path(__file__).parent
PROMPTS_DIR = PROJECT_DIR / "prompts"
WORK_DIR: Path  # set at startup: --workdir, or a fresh temp directory

MESSAGE = dict[str, str]

COLORS = {
    "orchestrator": "bold cyan",
    "scientist": "bold magenta",
    "general": "bold green",
    "executor": "bold yellow",
    "summarizer": "bold blue",
    "web search": "bold yellow",
    "human": "bold red",
    "system": "dim",
}

MAX_DECISIONS = 8  # Task 6 limit; 0 means no limit. Set with --max-decisions.
WEB_SEARCH_ROUNDS = 3  # search rounds the executor may use per turn
FINISH_REJECTIONS = 2  # times the orchestrator is re-asked after an unjustified finish


class DialogueState(BaseModel):
    messages: Annotated[list[MESSAGE], lambda old, new: old + new]


class OrchestratorDecision(BaseModel):
    next_role: Literal["scientist", "general", "executor", "human", "finish"]
    stop_kind: Literal["none", "ambiguous_query", "hard_decision"]
    pending_step: Literal["none", "executor", "finish"]
    instruction: str
    question: str
    reason: str

    @property
    def is_hard_decision(self) -> bool:
        return (
            self.next_role == "human"
            and self.stop_kind == "hard_decision"
            and self.pending_step != "none"
        )

    @property
    def wants_finish(self) -> bool:
        return self.next_role == "finish" or (
            self.is_hard_decision and self.pending_step == "finish"
        )


class HumanVerdict(BaseModel):
    approved: bool
    reason: str


PROMPT_MAP = {
    "orchestrator": (PROMPTS_DIR / "orc.md").read_text(),
    "scientist": (PROMPTS_DIR / "sci.md").read_text(),
    "general": (PROMPTS_DIR / "gen.md").read_text(),
    "executor": (PROMPTS_DIR / "exe.md").read_text(),
}

HUMAN_NOTE = """

## Human Replies

The orchestrator can stop and ask the user a question. The question is in the orchestrator message (`question`),
the user's answer follows it as a message with role `human`, and a refused step is recorded in a `system` message.
A `human` message is the user speaking: when it picks a reading of the request, follow that reading. A step the
user refused did not run, so do not act as if it did.
"""
for _role in ("scientist", "general", "executor"):
    PROMPT_MAP[_role] += HUMAN_NOTE


DATA_FILE = PROJECT_DIR / "SNLS80mV.mat"

DATA_NOTE = """

## Provided Data

`SNLS80mV.mat`, the Silverbox benchmark file named in Task 5, is already in the working directory, so no
web search or download is needed for it. Load it with `scipy.io.loadmat("SNLS80mV.mat")`: `V1` is the input
voltage u(t) and `V2` is the output voltage y(t), each a (1, 131072) float64 array, so take `["V1"][0]` and
`["V2"][0]`.
"""


def _is_mat_file(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as f:
        return f.read(6) == b"MATLAB"


def provide_data_file() -> bool:
    if not _is_mat_file(DATA_FILE):
        console.print(
            Panel(
                f"{DATA_FILE.name} is missing or is not a MAT file. The executor will have to find the data with web search.",
                title="[yellow]DATA[/]",
                border_style="yellow",
            )
        )
        return False
    shutil.copy2(DATA_FILE, WORK_DIR / DATA_FILE.name)
    for role in PROMPT_MAP:
        PROMPT_MAP[role] += DATA_NOTE
    return True


def stream_with_retry(model, messages, label, max_backoff=300):
    attempt = 1
    while True:
        try:
            full_text = ""
            start_time = time.time()
            with Live(
                Panel(
                    Text("...", style="dim"),
                    title=f"[{COLORS[label.lower()]}]{label}[/]",
                    subtitle=f"[dim]streaming...[/]",
                    border_style=COLORS[label.lower()],
                ),
                console=console,
                refresh_per_second=10,
                transient=True,
            ) as live:
                for chunk in model.stream(messages):
                    if chunk.content:
                        text_part = (
                            chunk.content
                            if isinstance(chunk.content, str)
                            else str(chunk.content)
                        )
                        full_text += text_part
                        elapsed = time.time() - start_time
                        preview = (
                            full_text[-500:] if len(full_text) > 500 else full_text
                        )
                        live.update(
                            Panel(
                                (
                                    Text(preview, style="white")
                                    if preview.strip()
                                    else Text("...", style="dim")
                                ),
                                title=f"[{COLORS[label.lower()]}]{label}[/]",
                                subtitle=f"[dim]{len(full_text)} chars · {elapsed:.1f}s[/]",
                                border_style=COLORS[label.lower()],
                            )
                        )
            return full_text, ""
        except Exception as exc:
            backoff = min(2**attempt, max_backoff)
            console.print(
                Panel(
                    f"[red]Failed: {exc}\nRetrying in {backoff}s (attempt {attempt})[/]",
                    title=f"[{COLORS[label.lower()]}]{label} · RETRY[/]",
                    border_style="red",
                )
            )
            time.sleep(backoff)
            attempt += 1


_script_counter = itertools.count(1)


def code_tool(code: str, timeout: int = 300) -> tuple[int, str, str]:
    # Skip numbers already used, so a reused --workdir never overwrites an earlier script.
    file_path = WORK_DIR / f"script_{next(_script_counter):03d}.py"
    while file_path.exists():
        file_path = WORK_DIR / f"script_{next(_script_counter):03d}.py"
    file_path.write_text(code, encoding="utf-8")
    try:
        result = subprocess.run(
            ["uv", "run", "--project", str(PROJECT_DIR), "python", str(file_path)],
            cwd=WORK_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode(errors="replace") if exc.stdout else ""
        return -1, stdout, f"Timed out after {timeout}s"
    return result.returncode, result.stdout, result.stderr


REQUIREMENT_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?"
    r"([<>=!~]=?[A-Za-z0-9.*+!_-]+(,[<>=!~]=?[A-Za-z0-9.*+!_-]+)*)?"
)


def dependency_tool(packages: list[str], timeout: int = 600) -> tuple[int, str, str]:
    invalid = [pkg for pkg in packages if not REQUIREMENT_RE.fullmatch(pkg)]
    if invalid:
        return -1, "", f"Rejected invalid package spec(s): {', '.join(invalid)}"
    try:
        result = subprocess.run(
            ["uv", "add", "--project", str(PROJECT_DIR), *packages],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"uv add timed out after {timeout}s"
    return result.returncode, result.stdout, result.stderr


def web_search_tool(query: str) -> str:
    model = ChatOpenRouter(
        model="google/gemma-4-31b-it",
        temperature=0.01,
        streaming=True,
        plugins=[{"id": "web", "max_results": 5}],
    )
    message = (
        f"{query}\n\nAnswer from the web search results. List every relevant source as "
        "`URL - what it contains`, and quote direct file download links exactly."
    )
    reply, _ = stream_with_retry(model, [HumanMessage(content=message)], "WEB SEARCH")
    return reply


def _extract_blocks(text: str) -> list[tuple[str, str]]:
    return re.findall(r"```(\w*)[^\S\n]*\n(.*?)```", text, re.DOTALL)


def _block_lines(blocks: list[tuple[str, str]], lang: str) -> list[str]:
    return [
        line.strip()
        for block_lang, body in blocks
        if block_lang.lower() == lang
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _workdir_listing(max_entries: int = 50) -> str:
    entries = sorted(
        path for path in WORK_DIR.rglob("*") if not path.name.startswith("script_")
    )
    lines = [
        f"- {path.relative_to(WORK_DIR)}{'/' if path.is_dir() else f' ({path.stat().st_size} bytes)'}"
        for path in entries[:max_entries]
    ]
    if len(entries) > max_entries:
        lines.append(f"- ... and {len(entries) - max_entries} more")
    listing = "\n".join(lines) or "(empty: no data has been downloaded yet)"
    return f"\nWorking directory for this run: {WORK_DIR}\nFiles in it (scripts excluded):\n{listing}\n"


def _decision_count(messages: list[MESSAGE]) -> int:
    return sum(1 for msg in messages if msg.get("role") == "orchestrator")


def _has_verified_rmse(messages: list[MESSAGE]) -> bool:
    for msg in reversed(messages):
        if msg.get("role") == "executor":
            run = re.search(
                r"^Exit code: 0\nStdout: (.*?)\nStderr:", msg.get("text", ""), re.M | re.S
            )
            return bool(run and re.search(r"rmse\D{0,40}\d", run.group(1), re.I))
    return False


ORCHESTRATOR_MAX_CONTEXT = 200_000


def _restart_orchestrator(schema: type[BaseModel] = OrchestratorDecision):
    return ChatOpenRouter(
        model="google/gemma-4-31b-it",
        temperature=0.2,
        max_tokens=ORCHESTRATOR_MAX_CONTEXT,
        streaming=False,
        plugins=[{"id": "context-compression", "enabled": True}],
    ).with_structured_output(schema, method="json_schema")


def _trim_dialogue(messages: list[MESSAGE], max_chars: int = 50000) -> list[MESSAGE]:
    trimmed = list(messages)
    while len(json.dumps(trimmed)) > max_chars and len(trimmed) > 1:
        trimmed = trimmed[1:]
    return trimmed


def _decide(
    prompt: str,
    schema: type[BaseModel] = OrchestratorDecision,
    status: str = "Orchestrator deciding...",
):
    model = _restart_orchestrator(schema)
    attempt = 1
    max_backoff = 300
    while True:
        try:
            with console.status(
                f"[{COLORS['orchestrator']}]{status}[/]",
                spinner="dots",
            ):
                return model.invoke([HumanMessage(content=prompt)])
        except Exception as exc:
            backoff = min(2**attempt, max_backoff)
            console.print(
                Panel(
                    f"[red]{exc}[/]",
                    title=f"[{COLORS['orchestrator']}]Orchestrator · FAILED (attempt {attempt})[/]",
                    border_style="red",
                )
            )
            console.print(
                Panel(
                    "Restarting orchestrator model...",
                    title="[bold cyan]ORCHESTRATOR · RESTART[/]",
                    border_style="yellow",
                )
            )
            model = _restart_orchestrator(schema)
            time.sleep(backoff)
            attempt += 1


FINISH_REJECTED_NOTE = """
Your previous decision was to finish (directly, or as the pending step of a hard decision), and it was rejected:
the most recent executor turn does not contain a test RMSE printed by a script that ran successfully. Choose
scientist, general, executor, or human with a different pending step instead.
"""

FALLBACK_INSTRUCTION = (
    "Implement the latest scientist plan, downloading any missing data into the "
    "working directory first, and print the free-run test RMSE."
)


def orchestrator_node(state: DialogueState) -> dict:
    trimmed_messages = _trim_dialogue(state.messages, max_chars=45000)
    number = _decision_count(state.messages) + 1
    budget = (
        f"\nThis is decision {number} of {MAX_DECISIONS}.\n"
        if MAX_DECISIONS
        else f"\nThis is decision {number}; there is no decision limit.\n"
    )
    prompt = (
        PROMPT_MAP["orchestrator"]
        + budget
        + _workdir_listing()
        + "\nDialogue:\n"
        + json.dumps(trimmed_messages)
    )

    decision = _decide(prompt)
    verified = _has_verified_rmse(state.messages)
    for _ in range(FINISH_REJECTIONS):
        if not decision.wants_finish or verified:
            break
        console.print(
            Panel(
                "No verified test RMSE yet. Asking the orchestrator to choose again.",
                title=f"[{COLORS['orchestrator']}]FINISH REJECTED[/]",
                border_style="yellow",
            )
        )
        decision = _decide(prompt + FINISH_REJECTED_NOTE)
    if decision.wants_finish and not verified:
        # Running the coding tool is itself a hard decision, so the override asks first.
        decision = OrchestratorDecision(
            next_role="human",
            stop_kind="hard_decision",
            pending_step="executor",
            instruction=FALLBACK_INSTRUCTION,
            question=(
                "No script has printed a verified test RMSE yet, so the dialogue cannot end. "
                f"The next step runs the coding tool: the executor will {FALLBACK_INSTRUCTION[0].lower()}"
                f"{FALLBACK_INSTRUCTION[1:]} Proceed?"
            ),
            reason="Finishing is not allowed until an executor run has printed a test RMSE.",
        )
    if decision.next_role != "human":
        decision = decision.model_copy(
            update={"stop_kind": "none", "pending_step": "none", "question": ""}
        )
    elif decision.stop_kind == "hard_decision" and decision.pending_step == "none":
        # A hard decision with no step to run can only be answered, like a query.
        decision = decision.model_copy(update={"stop_kind": "ambiguous_query"})

    table = Table(
        title=f"[{COLORS['orchestrator']}]Orchestrator Decision {number}[/]",
        box=box.ROUNDED,
        border_style=COLORS["orchestrator"],
    )
    table.add_column("Field", style="bold cyan")
    table.add_column("Value", style="white")
    table.add_row(
        "Next Role",
        f"[{COLORS.get(decision.next_role, 'white')}]{decision.next_role}[/]",
    )
    if decision.next_role == "human":
        table.add_row("Stop", decision.stop_kind.replace("_", " "))
        if decision.is_hard_decision:
            table.add_row("Pending Step", decision.pending_step)
        table.add_row("Question", decision.question)
    if decision.instruction:
        table.add_row("Instruction", decision.instruction)
    table.add_row("Reason", decision.reason)
    console.print(table)

    return {
        "messages": [{"role": "orchestrator", "text": decision.model_dump_json()}]
    }


def scientist_node(state: DialogueState) -> dict:
    model = ChatOpenRouter(
        model="google/gemma-4-31b-it",
        temperature=0.01,
        streaming=True,
    )
    prompt = PROMPT_MAP["scientist"] + "\nDialogue:\n" + json.dumps(state.messages)
    full_text, _ = stream_with_retry(model, [HumanMessage(content=prompt)], "SCIENTIST")
    console.print(
        Panel(
            f"[dim]{full_text[:400]}...[/]",
            title=f"[{COLORS['scientist']}]SCIENTIST · {len(full_text)} chars[/]",
            border_style=COLORS["scientist"],
        )
    )
    return {"messages": [{"role": "scientist", "text": full_text}]}


def general_node(state: DialogueState) -> dict:
    model = ChatOpenRouter(
        model="google/gemma-4-31b-it",
        temperature=0.01,
        streaming=True,
    )
    prompt = PROMPT_MAP["general"] + "\nDialogue:\n" + json.dumps(state.messages)
    full_text, _ = stream_with_retry(model, [HumanMessage(content=prompt)], "GENERAL")
    console.print(
        Panel(
            f"[dim]{full_text[:400]}...[/]",
            title=f"[{COLORS['general']}]GENERAL · {len(full_text)} chars[/]",
            border_style=COLORS["general"],
        )
    )
    return {"messages": [{"role": "general", "text": full_text}]}


EXECUTOR_CODE_FORMAT = """
## Response Format For This Turn

Reply with exactly one complete, self-contained Python script in a single ```python code block.
It will be saved to a new file and run for you with `uv run`; the exit code, stdout and stderr are reported back automatically.
The script must print the free-run test RMSE to stdout.

The script runs with the working directory for this run as its current directory, so relative paths read and
write there. Files saved by earlier scripts, such as downloaded data, stay there for later turns.

If the data the plan needs is not in the working directory and you do not know where to download it, reply with
only a ```search code block containing up to 3 web search queries, one per line, instead of a script. The results
are shown to you and you are asked again. Once you know the source, write a script that downloads the data into
the working directory (e.g. with urllib.request), unpacks it if needed, checks that it loaded correctly, and then
carries out the plan.

If the script imports third-party packages that may not be installed yet, put a ```dependencies code block
before the script, with one PyPI requirement per line (e.g. `numpy` or `torch>=2.4`). These are added to the
project with `uv add` before the script runs. Omit the block when no new packages are needed.
"""


def executor_node(state: DialogueState) -> dict:
    model = ChatOpenRouter(
        model="google/gemma-4-31b-it",
        temperature=0.01,
        streaming=True,
    )
    searches: list[tuple[str, str]] = []
    rounds_used = 0
    while True:
        rounds_left = WEB_SEARCH_ROUNDS - rounds_used
        search_text = "".join(
            f"\n### Query: {query}\n{result}\n" for query, result in searches
        )
        if searches:
            search_text = "\n## Web Search Results From This Turn\n" + search_text
        search_text += (
            f"\nWeb search rounds left this turn: {rounds_left}.\n"
            if rounds_left
            else "\nNo web search rounds are left this turn: reply with the script now.\n"
        )
        prompt = (
            PROMPT_MAP["executor"]
            + EXECUTOR_CODE_FORMAT
            + _workdir_listing()
            + search_text
            + "\nDialogue:\n"
            + json.dumps(state.messages)
        )
        reply, _ = stream_with_retry(model, [HumanMessage(content=prompt)], "EXECUTOR")
        blocks = _extract_blocks(reply)
        queries = _block_lines(blocks, "search")
        if not queries or not rounds_left:
            break
        for query in queries[:3]:
            searches.append((query, web_search_tool(query)))
        rounds_used += 1

    code_blocks = [body for lang, body in blocks if lang.lower() in ("python", "py", "")]
    code = max(code_blocks, key=len) if code_blocks else None
    packages = _block_lines(blocks, "dependencies")

    search_summary = "".join(
        f"Web search: {query}\n{result[:1500]}\n\n" for query, result in searches
    )
    deps_text = ""
    deps_ok = True
    if packages:
        with console.status(
            f"[{COLORS['executor']}]Adding dependencies: {', '.join(packages)}[/]",
            spinner="dots",
        ):
            deps_exit, _, deps_stderr = dependency_tool(packages)
        deps_ok = deps_exit == 0
        deps_text = f"Dependencies: {', '.join(packages)} ({'added' if deps_ok else 'failed'})\n"
        if not deps_ok:
            deps_text += f"uv add stderr: {deps_stderr}\n"

    if code is None:
        exit_code = -1
        result_text = (
            deps_text
            + "Exit code: n/a\nStdout: \nStderr: No ```python code block found in executor reply.\n"
            "Status: failure"
        )
    elif not deps_ok:
        exit_code = -1
        result_text = deps_text + "Script not run because dependencies could not be added.\nStatus: failure"
    else:
        with console.status(
            f"[{COLORS['executor']}]Executor running script...[/]", spinner="dots"
        ):
            exit_code, stdout, stderr = code_tool(code)
        result_text = deps_text + (
            f"Exit code: {exit_code}\nStdout: {stdout}\nStderr: {stderr}\n"
            f"Status: {'success' if exit_code == 0 else 'failure'}"
        )

    status_style = "green" if exit_code == 0 else "red"
    console.print(
        Panel(
            result_text,
            title=f"[{COLORS['executor']}]EXECUTOR · [{status_style}]{'OK' if exit_code == 0 else 'FAIL'}[/][/]",
            border_style=status_style,
        )
    )
    return {"messages": [{"role": "executor", "text": search_summary + result_text}]}


def _last_decision(messages: list[MESSAGE]) -> OrchestratorDecision:
    for msg in reversed(messages):
        if msg.get("role") == "orchestrator":
            return OrchestratorDecision.model_validate_json(msg["text"])
    raise ValueError("No orchestrator decision in the dialogue.")


VERDICT_PROMPT = """The orchestrator of a multi-agent team stopped and asked the user whether to carry out one step.

Step: {step}
Instruction for that step: {instruction}
Question asked: {question}
User's reply: {reply}

Did the user approve carrying out this step now? `approved` is true only if the reply clearly agrees to it.
A reply that refuses, postpones, asks for a different step or for changes to this one, or only asks a question
is not an approval. `reason` is one sentence."""


def _judge_reply(decision: OrchestratorDecision, reply: str) -> HumanVerdict:
    prompt = VERDICT_PROMPT.format(
        step=decision.pending_step,
        instruction=decision.instruction,
        question=decision.question,
        reply=reply,
    )
    return _decide(prompt, schema=HumanVerdict, status="Reading the reply...")


def _refusal_note(decision: OrchestratorDecision) -> str:
    if decision.pending_step == "finish":
        effect = "The dialogue did not end and no measurement was reported."
    else:
        effect = "The executor was not called: no script was written or run, and no file on disk changed."
    return (
        f"Step refused by the user, so it did not run. Refused step ({decision.pending_step}): "
        f"\"{decision.instruction}\". {effect} The turn returns to the orchestrator."
    )


def human_node(
    state: DialogueState,
) -> Command[Literal["orchestrator", "executor", "__end__"]]:
    # The node reruns from the top on resume, so everything before interrupt() only reads state.
    decision = _last_decision(state.messages)
    hard = decision.is_hard_decision
    reply = interrupt(
        {
            "kind": "hard_decision" if hard else "ambiguous_query",
            "question": decision.question,
            "reason": decision.reason,
            "pending_step": decision.pending_step if hard else "none",
            "instruction": decision.instruction if hard else "",
        }
    )
    reply = str(reply).strip()
    messages = [{"role": "human", "text": reply}]
    back = (
        END
        if MAX_DECISIONS and _decision_count(state.messages) >= MAX_DECISIONS
        else "orchestrator"
    )
    if not hard:
        return Command(update={"messages": messages}, goto=back)

    verdict = _judge_reply(decision, reply)
    if verdict.approved:
        console.print(
            Panel(
                f"{verdict.reason}\nRunning: {decision.pending_step}",
                title=f"[{COLORS['human']}]HUMAN · ACCEPTED[/]",
                border_style="green",
            )
        )
        goto = END if decision.pending_step == "finish" else decision.pending_step
        return Command(update={"messages": messages}, goto=goto)

    note = _refusal_note(decision)
    console.print(
        Panel(
            f"{verdict.reason}\n{note}",
            title=f"[{COLORS['human']}]HUMAN · REFUSED[/]",
            border_style="red",
        )
    )
    messages.append({"role": "system", "text": note})
    return Command(update={"messages": messages}, goto=back)


def route_decision(
    state: DialogueState,
) -> Literal["scientist", "general", "executor", "human", "finish"]:
    last = state.messages[-1] if state.messages else {"text": "{}"}
    try:
        d = json.loads(last.get("text", "{}"))
        return d.get("next_role", "scientist")
    except Exception:
        return "scientist"


def route_after_turn(state: DialogueState) -> Literal["orchestrator", "limit"]:
    if MAX_DECISIONS and _decision_count(state.messages) >= MAX_DECISIONS:
        return "limit"
    return "orchestrator"


BANNER = r"""
╔═════════════════════════════════════════════════════════╗
║            Photonics AI Solver Chain                    ║
║          multi-agent research pipeline                  ║
╚═════════════════════════════════════════════════════════╝
""".strip()


def build_graph():
    graph = StateGraph(DialogueState)
    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("scientist", scientist_node)
    graph.add_node("general", general_node)
    graph.add_node("executor", executor_node)
    graph.add_node("human", human_node)

    graph.add_conditional_edges(
        "orchestrator",
        lambda state: route_decision(state),
        {
            "scientist": "scientist",
            "general": "general",
            "executor": "executor",
            "human": "human",
            "finish": END,
        },
    )
    for role in ("scientist", "general", "executor"):
        graph.add_conditional_edges(
            role, route_after_turn, {"orchestrator": "orchestrator", "limit": END}
        )
    # human routes itself with Command: back to the orchestrator, or on to an accepted step.
    graph.set_entry_point("orchestrator")
    # The checkpointer keeps a paused thread's state until it is resumed with the reply.
    return graph.compile(checkpointer=InMemorySaver())


def ask_human(payload: dict) -> str:
    hard = payload["kind"] == "hard_decision"
    body = Text()
    body.append(
        "Hard decision\n" if hard else "Ambiguous query\n", style=COLORS["human"]
    )
    body.append(f"\n{payload['question']}\n", style="bold white")
    if hard:
        body.append(f"\nPending step: {payload['pending_step']}\n", style="white")
        body.append(f"Instruction: {payload['instruction']}\n", style="dim")
    body.append(f"\nWhy: {payload['reason']}", style="dim")
    console.print(
        Panel(
            body,
            title=f"[{COLORS['human']}]HUMAN INPUT NEEDED · graph paused[/]",
            subtitle="[dim]type your reply; Ctrl+C stops the run[/]",
            border_style="red",
        )
    )
    while True:
        reply = console.input(f"[{COLORS['human']}]you › [/]").strip()
        if reply:
            return reply


def run_thread(compiled, graph_input, config: dict, ask=ask_human) -> None:
    """Run one thread until it ends, resuming it with the human's reply at every pause."""
    while True:
        for _ in compiled.stream(graph_input, config=config, stream_mode="values"):
            pass
        pending = compiled.get_state(config).interrupts
        if not pending:
            return
        graph_input = Command(resume=ask(pending[0].value))


def write_transcript(messages: list[MESSAGE], thread_id: str) -> Path:
    path = Path("transcripts") / f"{thread_id}.md"
    path.parent.mkdir(exist_ok=True)
    parts = [f"# Transcript: thread `{thread_id}`\n"]
    for number, msg in enumerate(messages, 1):
        text = msg.get("text", "")
        if msg.get("role") == "orchestrator":
            text = "```json\n" + json.dumps(json.loads(text), indent=2) + "\n```"
        parts.append(f"## {number}. {msg.get('role')}\n\n{text}\n")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Photonics AI Solver Chain")
    parser.add_argument(
        "--max-decisions",
        type=int,
        default=MAX_DECISIONS,
        help="orchestrator decision limit (default: %(default)s, per Task 6); 0 means no limit",
    )
    parser.add_argument(
        "--request",
        help="your request to the orchestrator; task.pdf is then given as background. "
        "Without it, task.pdf itself is the request",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        help="working directory to use (and keep results in) across runs; default: a new temp directory",
    )
    parser.add_argument(
        "--thread",
        default=None,
        help="LangGraph thread id (default: a new one per run)",
    )
    args = parser.parse_args()
    MAX_DECISIONS = args.max_decisions
    if args.workdir:
        WORK_DIR = args.workdir.resolve()
        WORK_DIR.mkdir(parents=True, exist_ok=True)
    else:
        WORK_DIR = Path(tempfile.mkdtemp(prefix="photonics_"))
    thread_id = args.thread or f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

    console.print(Panel(BANNER, style="bold cyan", border_style="cyan"))
    console.print(f"[dim]Thread: {thread_id}   Working directory: {WORK_DIR}[/]")
    provide_data_file()

    compiled = build_graph()

    import pymupdf

    pdf_path = Path("task.pdf")
    if pdf_path.exists():
        doc = pymupdf.open(str(pdf_path))
        pdf_text = ""
        for page in doc:
            pdf_text += page.get_text()
        doc.close()
        user_text = pdf_text.strip()
    else:
        user_text = "Perform Task 5: differential equation, network plan, script, and test RMSE."

    if args.request:
        messages = [
            {"role": "user", "text": "Background, the Homework 1 task this team works on:\n\n" + user_text},
            {"role": "user", "text": args.request},
        ]
    else:
        messages = [{"role": "user", "text": user_text}]
    initial_state: DialogueState = DialogueState(messages=messages)
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 10_000}

    try:
        run_thread(compiled, initial_state, config)
        final_messages = compiled.get_state(config).values["messages"]
        if (
            MAX_DECISIONS
            and _decision_count(final_messages) >= MAX_DECISIONS
            and not _has_verified_rmse(final_messages)
        ):
            console.print(
                Panel(
                    f"Decision limit ({MAX_DECISIONS}) reached without a verified test RMSE. "
                    "Summarizing the work so far; rerun with --max-decisions 0 to keep going until done.",
                    title="[yellow]DECISION LIMIT[/]",
                    border_style="yellow",
                )
            )
    except (KeyboardInterrupt, EOFError):
        console.print(
            Panel(
                "Interrupted. Summarizing the work so far.",
                title="[yellow]INTERRUPTED[/]",
                border_style="yellow",
            )
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
        import traceback

        console.print(
            Panel(
                traceback.format_exc(), title="[red]GRAPH HALTED[/]", border_style="red"
            )
        )

    final_messages = compiled.get_state(config).values.get("messages") or initial_state.messages
    transcript_path = write_transcript(final_messages, thread_id)
    console.print(f"[dim]Dialogue transcript saved to {transcript_path}[/]")

    # ── Final Summary ──
    console.print(
        Panel(
            "Generating final report from scientist + executor findings...",
            title="[bold blue]SUMMARIZER[/]",
            border_style="blue",
        )
    )

    output_path = Path("OUTPUT.md")

    if not final_messages:
        final_messages = [{"role": "system", "text": "No output produced."}]

    scientist_outputs = [
        msg.get("text", "")
        for msg in final_messages
        if msg.get("role") in ("scientist", "executor")
    ]
    raw_findings = (
        "\n\n".join(scientist_outputs) if scientist_outputs else "No findings."
    )

    summary_model = ChatOpenRouter(
        model="google/gemma-4-31b-it",
        temperature=0.2,
        streaming=True,
    )

    SUMMARY_PROMPT = f"""You are a report writer. Below are the raw findings from a scientist agent working on a photonics task, plus any executor test results.

Produce a clean, well-structured Markdown report with these sections:
1. **Task Summary** — what was asked
2. **Methodology** — approach taken
3. **Results** — key findings and numbers (especially RMSE)
4. **Conclusion** — final assessment

Use proper Markdown formatting (headings, code blocks, lists). Be concise but thorough.

Raw findings:
{raw_findings[:30000]}

Report:"""

    summary_text, _ = stream_with_retry(
        summary_model, [HumanMessage(content=SUMMARY_PROMPT)], "SUMMARIZER"
    )

    output_path.write_text(summary_text, encoding="utf-8")
    console.print(
        Panel(
            f"[green]Report saved to [bold]{output_path}[/][/]",
            title="[bold blue]COMPLETE[/]",
            border_style="green",
        )
    )
