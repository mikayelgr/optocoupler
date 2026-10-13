"""The graph state and the structured outputs the models return."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel

MESSAGE = dict[str, Any]  # {"role", "text"}, plus "tool_calls" on an executor message that calls the bench


class RequestScope(BaseModel):
    tasks: list[str]  # the task files (in task/) the request refers to
    result: Literal["none", "script", "bench"]
    quantity: str
    reason: str

    @property
    def requirement(self) -> str:
        # What the most recent executor turn must contain before the dialogue may end.
        if self.result == "script":
            return f"a value labelled {self.quantity}, printed by a script that ran successfully"
        if self.result == "bench":
            return "a light reading from the bench, taken after its last bench change"
        return "nothing: no measured value is needed"


class DialogueState(BaseModel):
    messages: Annotated[list[MESSAGE], lambda old, new: old + new]
    scope: dict | None = None  # a RequestScope, kept as a plain dict like the messages


def load_scope(scope: dict | None) -> RequestScope | None:
    return RequestScope.model_validate(scope) if scope else None


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


def decision_count(messages: list[MESSAGE]) -> int:
    return sum(1 for msg in messages if msg.get("role") == "orchestrator")


def last_decision(messages: list[MESSAGE]) -> OrchestratorDecision:
    for msg in reversed(messages):
        if msg.get("role") == "orchestrator":
            return OrchestratorDecision.model_validate_json(msg["text"])
    raise ValueError("No orchestrator decision in the dialogue.")
