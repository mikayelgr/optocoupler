"""The orchestrator: one structured decision per turn, with the finish guard."""

from __future__ import annotations

import json

from rich import box
from rich.panel import Panel
from rich.table import Table

from . import llm
from .config import COLORS, FINISH_REJECTIONS, RUN, console
from .prompts import ROLE_PROMPTS
from .scope import has_required_result
from .state import (
    MESSAGE,
    DialogueState,
    OrchestratorDecision,
    RequestScope,
    decision_count,
    load_scope,
)
from .tools import workdir_listing

REQUIRED_RESULT = {
    "none": """
## Required Result

The request does not ask for a measured value. You may finish as soon as the dialogue answers it, without a
hard-decision stop. Send work to the executor only if answering really needs code to run{bench}.
""",
    "script": """
## Required Result

The request asks for a measured value: **{quantity}**. You may finish (directly, or as the `pending_step` of a hard
decision) only once the most recent executor turn contains a {quantity} value printed by a script that ran
successfully. Report that printed value, never an estimate.
""",
    "bench": """
## Required Result

The request is about the light bench. The executor carries it out with its bench tools (directly, or from a script
through `bench_tools`), so it needs no plan from the scientist and no answer from the general agent: send it to the
executor (through the hard-decision stop).
You may finish (directly, or as the `pending_step` of a hard decision) only once the most recent executor turn ended its
bench work with a light reading (`read_lux`). Report that measured value.
""",
}

FINISH_REJECTED_NOTE = """
Your previous decision was to finish (directly, or as the pending step of a hard decision), and it was rejected:
the most recent executor turn does not contain {requirement}. Choose scientist, general, executor, or human with a
different pending step instead.
"""

FALLBACK_INSTRUCTION = {
    "script": (
        "Carry out the latest plan for the request, downloading any missing data into the working directory "
        "first, and print the {quantity}."
    ),
    "bench": "Carry out the latest request on the light bench, and end with a light reading.",
}


def _required_result(scope: RequestScope | None) -> str:
    if scope is None:
        return ""
    return REQUIRED_RESULT[scope.result].format(
        quantity=scope.quantity, bench=" or drive the bench" if RUN.bench else ""
    )


def _trim_dialogue(messages: list[MESSAGE], max_chars: int = 50000) -> list[MESSAGE]:
    trimmed = list(messages)
    while len(json.dumps(trimmed)) > max_chars and len(trimmed) > 1:
        trimmed = trimmed[1:]
    return trimmed


def _show_decision(decision: OrchestratorDecision, number: int) -> None:
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


def orchestrator_node(state: DialogueState) -> dict:
    scope = load_scope(state.scope)
    trimmed_messages = _trim_dialogue(state.messages, max_chars=45000)
    number = decision_count(state.messages) + 1
    budget = (
        f"\nThis is decision {number} of {RUN.max_decisions}.\n"
        if RUN.max_decisions
        else f"\nThis is decision {number}; there is no decision limit.\n"
    )
    prompt = (
        ROLE_PROMPTS["orchestrator"]
        + _required_result(scope)
        + budget
        + workdir_listing()
        + "\nDialogue:\n"
        + json.dumps(trimmed_messages)
    )

    decision = llm.decide(prompt, schema=OrchestratorDecision)
    verified = has_required_result(state.messages, scope)
    for _ in range(FINISH_REJECTIONS):
        if not decision.wants_finish or verified:
            break
        console.print(
            Panel(
                f"The latest executor turn does not contain {scope.requirement}. Asking the orchestrator to choose again.",
                title=f"[{COLORS['orchestrator']}]FINISH REJECTED[/]",
                border_style="yellow",
            )
        )
        decision = llm.decide(
            prompt + FINISH_REJECTED_NOTE.format(requirement=scope.requirement),
            schema=OrchestratorDecision,
        )
    if decision.wants_finish and not verified:
        # Running the executor is itself a hard decision, so the override asks first.
        fallback = FALLBACK_INSTRUCTION[scope.result].format(quantity=scope.quantity)
        decision = OrchestratorDecision(
            next_role="human",
            stop_kind="hard_decision",
            pending_step="executor",
            instruction=fallback,
            question=(
                f"The dialogue cannot end yet: it needs {scope.requirement}. "
                f"The next step runs the executor: it will {fallback[0].lower()}"
                f"{fallback[1:]} Proceed?"
            ),
            reason=f"Finishing is not allowed until the most recent executor turn contains {scope.requirement}.",
        )
    if decision.next_role != "human":
        decision = decision.model_copy(
            update={"stop_kind": "none", "pending_step": "none", "question": ""}
        )
    elif decision.stop_kind == "hard_decision" and decision.pending_step == "none":
        # A hard decision with no step to run can only be answered, like a query.
        decision = decision.model_copy(update={"stop_kind": "ambiguous_query"})

    _show_decision(decision, number)
    return {"messages": [{"role": "orchestrator", "text": decision.model_dump_json()}]}
