"""The executor (the only role with tools) and the hardware node that runs its bench calls."""

from __future__ import annotations

import json
import re

from rich.panel import Panel

from . import llm
from .bench import format_call
from .config import COLORS, HARDWARE_ROUNDS, RUN, WEB_SEARCH_ROUNDS, console
from .prompts import ROLE_PROMPTS
from .state import MESSAGE, DialogueState, load_scope
from .tools import code_tool, dependency_tool, web_search_tool, workdir_listing

EXECUTOR_CODE_FORMAT = """
## Response Format For This Turn

{reply} with exactly one complete, self-contained Python script in a single ```python code block.
It will be saved to a new file and run for you; the exit code, stdout and stderr are reported back automatically.
The script must print the values the step asks for to stdout, each with its label (for example `RMSE: 0.0123`).

The script runs with the working directory for this run as its current directory, so relative paths read and
write there. Files saved by earlier scripts, such as downloaded data, stay there for later turns.

If the data the plan needs is not in the working directory and you do not know where to download it, reply with
only a ```search code block containing up to 3 web search queries, one per line, instead of a script. The results
are shown to you and you are asked again. Once you know the source, write a script that downloads the data into
the working directory (e.g. with urllib.request), unpacks it if needed, checks that it loaded correctly, and then
carries out the plan.

If the script imports third-party packages that may not be installed yet, put a ```dependencies code block
before the script, with one PyPI requirement per line (e.g. `numpy` or `torch>=2.4`). These are installed before
the script runs. Omit the block when no new packages are needed.
"""

EXECUTOR_BENCH_FORMAT = """
## Light Bench

You also control a light bench through tools: an LED driven by a Raspberry Pi Pico, and a phone that measures
the light level in lux.

- `set_led(percent)`: set the LED brightness, an integer from 0 (off) to 100 (full).
- `led_off()`: turn the LED off.
- `read_lux()`: read the current light level in lux.

There are two ways to reach the bench, and no others: no other Python module, device file or serial port reaches
it, so never guess a library name.

1. **Tool calls**, for a few steps of adjusting and measuring. The calls in one reply run one
after another, in the order you give them. Their results come back to you in a `hardware` message, and then you
are asked again, so you can adjust and measure again. Read the light level after every change instead of
predicting it: the LED's response is not linear, and room light adds to it. Never report a lux value that
`read_lux` did not return.

2. **The `bench_tools` module inside a script**, for sweeps or many measurements. It is already in the working
   directory and uses only the standard library, so it needs no `dependencies` block:

   ```python
   import bench_tools
   bench_tools.led_off()          # returns "LED off (Pico: OK 0)"
   bench_tools.set_led(40)        # returns "LED set to 40% (Pico: OK 40)"
   lux = bench_tools.read_lux()   # returns a float, such as 19.0
   ```

   Its calls go to the same tools, and they are written to the dialogue as a `hardware` message after the script's
   result. A call the bench rejects raises `RuntimeError`.

   Timing: no bench call waits. `set_led` and `led_off` take a few milliseconds, so a script can pulse or blink
   the LED by timing them itself, and `read_lux` returns the sensor's latest value at once.

When the bench step is done, reply without tool calls and without a code block: a short report of the final LED
setting and the last measured lux. The last bench call before that report, by a tool call or a script, must be
`read_lux`.
"""


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


def _turn_hardware(messages: list[MESSAGE]) -> list[MESSAGE]:
    # Hardware messages of the current executor turn, which starts after the last orchestrator decision.
    turn = []
    for msg in reversed(messages):
        if msg.get("role") == "orchestrator":
            break
        if msg.get("role") == "hardware":
            turn.append(msg)
    return turn[::-1]


def executor_node(state: DialogueState) -> dict:
    model = llm.chat_model()
    scope = load_scope(state.scope)
    quantity_text = (
        f"\nThe request asks for a measured {scope.quantity}: print it with the label "
        f"`{scope.quantity}`, followed by its value.\n"
        if scope and scope.result == "script"
        else ""
    )
    bench_text = ""
    bench_used = bool(_turn_hardware(state.messages))
    if RUN.bench:
        # Only the executor gets the bench tools, and only while it has bench rounds left this turn.
        hardware_left = HARDWARE_ROUNDS - len(_turn_hardware(state.messages))
        bench_text = EXECUTOR_BENCH_FORMAT + (
            f"\nBench rounds left this turn: {hardware_left}.\n"
            if hardware_left > 0
            else "\nNo bench rounds are left this turn: report the last measured lux now.\n"
        )
        if hardware_left > 0:
            # A bench request opens its executor turn with a tool call, never a script. The model still picks
            # the tool and its arguments; later replies may also be the plain-text report that ends the turn.
            must_call = scope is not None and scope.result == "bench" and not bench_used
            model = model.bind_tools(RUN.bench.tools, tool_choice="required" if must_call else None)
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
        # With the bench, its tools come first; a script is for computation or for many bench measurements.
        code_format = EXECUTOR_CODE_FORMAT.format(
            reply="When the step needs a script (for computation, or for many bench measurements), reply"
            if RUN.bench
            else "Reply"
        )
        prompt = (
            ROLE_PROMPTS["executor"]
            + bench_text
            + code_format
            + quantity_text
            + workdir_listing()
            + search_text
            + "\nDialogue:\n"
            + json.dumps(state.messages)
        )
        reply, message = llm.stream_with_retry(model, prompt, "EXECUTOR")
        tool_calls = getattr(message, "tool_calls", None) or []
        blocks = _extract_blocks(reply)
        queries = _block_lines(blocks, "search")
        if tool_calls or not queries or not rounds_left:
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
    if tool_calls:
        # Hardware turn: the hardware node runs these calls and the executor is asked again.
        calls = [{"name": call["name"], "args": call["args"]} for call in tool_calls]
        text = search_summary + (f"{reply.strip()}\n" if reply.strip() else "")
        text += "Bench calls: " + "; ".join(format_call(call) for call in calls)
        console.print(
            Panel(
                text,
                title=f"[{COLORS['executor']}]EXECUTOR · BENCH CALLS[/]",
                border_style=COLORS["executor"],
            )
        )
        return {"messages": [{"role": "executor", "text": text, "tool_calls": calls}]}

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
            deps_text += f"Install stderr: {deps_stderr}\n"

    if code is None and bench_used and not packages:
        # The executor closes a bench turn with a plain-text report instead of a script.
        exit_code = 0
        result_text = f"Bench report: {reply.strip()}\nStatus: success"
    elif code is None:
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
        if RUN.bench:
            RUN.bench.take_script_calls()  # drop calls left over from an earlier script
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
    messages = [{"role": "executor", "text": search_summary + result_text}]
    script_calls = RUN.bench.take_script_calls() if RUN.bench and code is not None else []
    if script_calls:
        # The script's bench calls go into the dialogue like a hardware turn, so the next turn reads them.
        text = "Bench calls made by the script:\n" + "\n".join(script_calls)
        failed = any(" -> Error" in line for line in script_calls)
        console.print(
            Panel(text, title=f"[{COLORS['hardware']}]HARDWARE · from script[/]", border_style="red" if failed else "green")
        )
        messages.append({"role": "hardware", "text": text})
    return {"messages": messages}


def hardware_node(state: DialogueState) -> dict:
    # Runs the executor's bench calls one by one, in order, and writes the results to the dialogue.
    lines = []
    for call in state.messages[-1].get("tool_calls", []):
        with console.status(
            f"[{COLORS['hardware']}]Bench: {format_call(call)}[/]", spinner="dots"
        ):
            result = RUN.bench.call(call["name"], call["args"])
        lines.append(f"{format_call(call)} -> {result}")
    text = "\n".join(lines) or "No bench calls were made."
    failed = any(" -> Error" in line for line in lines)
    console.print(
        Panel(
            text,
            title=f"[{COLORS['hardware']}]HARDWARE[/]",
            border_style="red" if failed else "green",
        )
    )
    return {"messages": [{"role": "hardware", "text": text}]}
