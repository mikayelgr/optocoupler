"""The LangGraph graph: nodes, routing, and running a thread through its human stops and cancellations."""

from __future__ import annotations

import json
from typing import Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command

from .config import RUN
from .executor import executor_node, hardware_node
from .human import ask_human, cancel_messages, human_node, steer_node
from .orchestrator import orchestrator_node
from .roles import general_node, scientist_node
from .state import DialogueState, decision_count


def route_decision(
    state: DialogueState,
) -> Literal["scientist", "general", "executor", "human", "finish"]:
    last = state.messages[-1] if state.messages else {"text": "{}"}
    try:
        return json.loads(last.get("text", "{}")).get("next_role", "scientist")
    except Exception:
        return "scientist"


def route_after_turn(state: DialogueState) -> Literal["orchestrator", "limit"]:
    if RUN.max_decisions and decision_count(state.messages) >= RUN.max_decisions:
        return "limit"
    return "orchestrator"


def route_after_executor(
    state: DialogueState,
) -> Literal["hardware", "orchestrator", "limit"]:
    if state.messages[-1].get("tool_calls"):
        return "hardware"
    return route_after_turn(state)


def build_graph():
    graph = StateGraph(DialogueState)
    graph.add_node("orchestrator", orchestrator_node)
    graph.add_node("scientist", scientist_node)
    graph.add_node("general", general_node)
    graph.add_node("executor", executor_node)
    graph.add_node("human", human_node)
    # "cancel" never runs: run_thread writes the note of a cancelled step as this node, which leads to the steer stop.
    graph.add_node("cancel", lambda state: {})
    graph.add_node("steer", steer_node)
    graph.add_edge("cancel", "steer")
    if RUN.bench:
        graph.add_node("hardware", hardware_node)

    graph.add_conditional_edges(
        "orchestrator",
        route_decision,
        {
            "scientist": "scientist",
            "general": "general",
            "executor": "executor",
            "human": "human",
            "finish": END,
        },
    )
    for role in ("scientist", "general") if RUN.bench else ("scientist", "general", "executor"):
        graph.add_conditional_edges(
            role, route_after_turn, {"orchestrator": "orchestrator", "limit": END}
        )
    if RUN.bench:
        # Hardware turn: the bench call's result goes to the dialogue and the executor decides again.
        graph.add_conditional_edges(
            "executor",
            route_after_executor,
            {"hardware": "hardware", "orchestrator": "orchestrator", "limit": END},
        )
        graph.add_edge("hardware", "executor")
    # human routes itself with Command: back to the orchestrator, or on to an accepted step. steer goes to the orchestrator.
    graph.set_entry_point("orchestrator")
    # The checkpointer keeps a paused thread's state until it is resumed with the reply.
    return graph.compile(checkpointer=InMemorySaver())


def cancel_step(compiled, config: dict) -> bool:
    """Ctrl+C during a step: drop it and pause the thread for a steering note. False when the run should stop."""
    snapshot = compiled.get_state(config)
    messages = snapshot.values.get("messages", [])
    if not snapshot.next or (RUN.max_decisions and decision_count(messages) >= RUN.max_decisions):
        return False  # nothing was running, or no decision is left to follow a note
    # Written as the "cancel" node, the note replaces the step: the cancelled node is not run again.
    compiled.update_state(config, {"messages": cancel_messages(snapshot.next[0])}, as_node="cancel")
    return True


def run_thread(compiled, graph_input, config: dict, ask=ask_human) -> None:
    """Run one thread until it ends, resuming it with the human's reply at every pause.

    Ctrl+C during a step cancels that step, not the run: the thread pauses for a note that steers the team.
    Ctrl+C at a prompt stops the run.
    """
    while True:
        try:
            for _ in compiled.stream(graph_input, config=config, stream_mode="values"):
                pass
        except KeyboardInterrupt:
            if not cancel_step(compiled, config):
                raise
            graph_input = None
            continue
        pending = compiled.get_state(config).interrupts
        if not pending:
            return
        graph_input = Command(resume=ask(pending[0].value))
