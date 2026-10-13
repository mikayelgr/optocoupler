"""Model calls: streamed replies and structured decisions, both retried until they succeed."""

from __future__ import annotations

import time

from langchain_core.messages import HumanMessage
from langchain_openrouter import ChatOpenRouter
from pydantic import BaseModel
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .config import COLORS, MODEL, ORCHESTRATOR_MAX_CONTEXT, console


def chat_model(temperature: float = 0.01, **kwargs) -> ChatOpenRouter:
    return ChatOpenRouter(model=MODEL, temperature=temperature, streaming=True, **kwargs)


def stream_with_retry(model, prompt: str, label: str, max_backoff: int = 300):
    """Stream one reply in a live panel. Returns the text and the whole message (which carries any tool calls)."""
    color = COLORS[label.lower()]
    attempt = 1
    while True:
        try:
            full_text = ""
            message = None
            start_time = time.time()
            with Live(
                Panel(
                    Text("...", style="dim"),
                    title=f"[{color}]{label}[/]",
                    subtitle="[dim]streaming...[/]",
                    border_style=color,
                ),
                console=console,
                refresh_per_second=10,
                transient=True,
            ) as live:
                for chunk in model.stream([HumanMessage(content=prompt)]):
                    message = chunk if message is None else message + chunk
                    if chunk.content:
                        text_part = (
                            chunk.content
                            if isinstance(chunk.content, str)
                            else str(chunk.content)
                        )
                        full_text += text_part
                        elapsed = time.time() - start_time
                        preview = full_text[-500:]
                        live.update(
                            Panel(
                                (
                                    Text(preview, style="white")
                                    if preview.strip()
                                    else Text("...", style="dim")
                                ),
                                title=f"[{color}]{label}[/]",
                                subtitle=f"[dim]{len(full_text)} chars · {elapsed:.1f}s[/]",
                                border_style=color,
                            )
                        )
            return full_text, message
        except Exception as exc:
            backoff = min(2**attempt, max_backoff)
            console.print(
                Panel(
                    f"[red]Failed: {exc}\nRetrying in {backoff}s (attempt {attempt})[/]",
                    title=f"[{color}]{label} · RETRY[/]",
                    border_style="red",
                )
            )
            time.sleep(backoff)
            attempt += 1


def _structured_model(schema: type[BaseModel]):
    return ChatOpenRouter(
        model=MODEL,
        temperature=0.2,
        max_tokens=ORCHESTRATOR_MAX_CONTEXT,
        streaming=False,
        plugins=[{"id": "context-compression", "enabled": True}],
    ).with_structured_output(schema, method="json_schema")


def decide(prompt: str, schema: type[BaseModel], status: str = "Orchestrator deciding..."):
    """One structured call (orchestrator decision, request scope, reply verdict); a failing model is recreated."""
    model = _structured_model(schema)
    attempt = 1
    max_backoff = 300
    while True:
        try:
            with console.status(f"[{COLORS['orchestrator']}]{status}[/]", spinner="dots"):
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
            model = _structured_model(schema)
            time.sleep(backoff)
            attempt += 1
