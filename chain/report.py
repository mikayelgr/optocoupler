"""What a run leaves behind: the dialogue transcript and the OUTPUT.md report."""

from __future__ import annotations

import json
from pathlib import Path

from rich.panel import Panel

from . import llm
from .config import console
from .scope import request_text
from .state import MESSAGE

SUMMARY_PROMPT = """You are a report writer. Below is a request to a multi-agent team, and the team's work on it:
plans from the scientist, explanations from the general agent, script runs from the executor and any light-bench
readings, each headed by its role.

Produce a clean, well-structured Markdown report with these sections:
1. **Request** — what was asked
2. **Approach** — how the team went about it
3. **Results** — key findings and numbers. Quote only values that a script printed or the bench measured, and say
   which ones are missing
4. **Conclusion** — final assessment

Use proper Markdown formatting (headings, code blocks, lists). Be concise but thorough.

Request:
{request}

Raw findings:
{findings}

Report:"""


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


def write_report(messages: list[MESSAGE], path: Path = Path("OUTPUT.md")) -> Path:
    console.print(
        Panel(
            "Generating the final report from the team's work...",
            title="[bold blue]SUMMARIZER[/]",
            border_style="blue",
        )
    )
    findings = [
        f"[{msg.get('role')}]\n{msg.get('text', '')}"
        for msg in messages
        if msg.get("role") in ("scientist", "general", "executor", "hardware")
    ]
    prompt = SUMMARY_PROMPT.format(
        request=request_text(messages)[:5000],
        findings=("\n\n".join(findings) if findings else "No findings.")[:30000],
    )
    summary_text, _ = llm.stream_with_retry(llm.chat_model(temperature=0.2), prompt, "SUMMARIZER")
    path.write_text(summary_text, encoding="utf-8")
    console.print(
        Panel(
            f"[green]Report saved to [bold]{path}[/][/]",
            title="[bold blue]COMPLETE[/]",
            border_style="green",
        )
    )
    return path
