"""The human-in-the-loop stops: the graph pauses, asks the user, and routes on the reply.

Two kinds: the orchestrator's own stop (human_node), and the steering stop after Ctrl+C cancels a step (steer_node).
"""

from __future__ import annotations

from typing import Literal

from langgraph.graph import END
from langgraph.types import Command, interrupt
from rich.panel import Panel
from rich.text import Text

from . import llm
from .config import COLORS, RUN, console
from .scope import drop_bench, request_text, scope_request, show_scope
from .state import DialogueState, HumanVerdict, OrchestratorDecision, decision_count, last_decision, load_scope

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
    return llm.decide(prompt, schema=HumanVerdict, status="Reading the reply...")


CANCELLED_STEP = {
    "orchestrator": "the orchestrator's decision",
    "scientist": "the scientist's reply",
    "general": "the general agent's reply",
    "executor": "the executor's turn",
    "hardware": "the executor's bench calls",
    "human": "the reading of the user's reply",
    "steer": "the reading of the user's steering note",
}


def cancel_messages(node: str) -> list[dict]:
    """What the dialogue records when Ctrl+C cancels a step: the bench calls it made, and a note."""
    messages = []
    script_calls = RUN.bench.take_script_calls() if RUN.bench else []
    if script_calls:
        messages.append({"role": "hardware", "text": "Bench calls made by the cancelled script:\n" + "\n".join(script_calls)})
    text = f"The user cancelled {CANCELLED_STEP.get(node, node)} before it finished, so its reply was discarded."
    if node in ("executor", "hardware"):
        text += (
            " Any script or bench call it had started was stopped; files it already wrote and bench settings it "
            "already made stay as they are."
        )
    messages.append({"role": "system", "text": text + " The user's next message steers the work."})
    return messages


def _rescope(state: DialogueState, messages: list[dict], title: str) -> dict | None:
    # The user's words can change the reading of the request, and with it the result the run must end with.
    scope = load_scope(state.scope)
    if scope is None:
        return None
    scope = scope_request(request_text(state.messages + messages), RUN.tasks, fixed_tasks=scope.tasks)
    if scope.result == "bench" and not RUN.bench:
        scope = drop_bench(scope)  # the bench can't be connected in the middle of a run
    show_scope(scope, title=title)
    return scope.model_dump()


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
    decision = last_decision(state.messages)
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
        if RUN.max_decisions and decision_count(state.messages) >= RUN.max_decisions
        else "orchestrator"
    )
    if not hard:
        # The answer picks a reading of the request, which can change the result it must end with.
        scope = _rescope(state, messages, title="REQUEST SCOPE · after your answer")
        return Command(update={"messages": messages, "scope": scope}, goto=back)

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


def steer_node(state: DialogueState) -> Command[Literal["orchestrator"]]:
    # run_thread sends the thread here after Ctrl+C cancels a step, with the cancel note as the last message.
    reply = interrupt({"kind": "steer", "note": state.messages[-1]["text"]})
    messages = [{"role": "human", "text": str(reply).strip()}]
    scope = _rescope(state, messages, title="REQUEST SCOPE · after your steering note")
    return Command(update={"messages": messages, "scope": scope}, goto="orchestrator")


def _ask_steer(payload: dict) -> str:
    console.print(
        Panel(
            Text(payload["note"], style="white")
            + Text("\n\nWhat should the team do instead? The orchestrator decides next, with your note.", style="bold white"),
            title=f"[{COLORS['human']}]CANCELLED · steer the team[/]",
            subtitle="[dim]type your note; Ctrl+C stops the run[/]",
            border_style="yellow",
        )
    )
    while True:
        reply = console.input(f"[{COLORS['human']}]you › [/]").strip()
        if reply:
            return reply


def ask_human(payload: dict) -> str:
    """Show the paused graph's question in the terminal and read the reply."""
    if payload["kind"] == "steer":
        return _ask_steer(payload)
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
