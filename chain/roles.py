"""The scientist and the general agent: each reads the whole dialogue and appends one message."""

from __future__ import annotations

import json

from rich.panel import Panel

from . import llm
from .config import COLORS, console
from .prompts import ROLE_PROMPTS
from .state import DialogueState


def _reply(role: str, state: DialogueState) -> dict:
    prompt = ROLE_PROMPTS[role] + "\nDialogue:\n" + json.dumps(state.messages)
    full_text, _ = llm.stream_with_retry(llm.chat_model(), prompt, role.upper())
    console.print(
        Panel(
            f"[dim]{full_text[:400]}...[/]",
            title=f"[{COLORS[role]}]{role.upper()} · {len(full_text)} chars[/]",
            border_style=COLORS[role],
        )
    )
    return {"messages": [{"role": role, "text": full_text}]}


def scientist_node(state: DialogueState) -> dict:
    return _reply("scientist", state)


def general_node(state: DialogueState) -> dict:
    return _reply("general", state)
