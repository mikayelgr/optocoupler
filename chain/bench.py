"""MCP client for the light bench (bench/server.py)."""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import sys
import threading
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client
from rich.panel import Panel

from .config import BENCH_HELPER, BENCH_SERVER, COLORS, PROJECT_DIR, RUN, console
from .prompts import BENCH_NOTE, BENCH_ROLE_NOTE, add_note


class Bench:
    """The light-bench MCP server, open for the whole run.

    One persistent session, so the Pico's serial port stays open between tool calls. The graph runs
    synchronously, so the session lives on its own event loop in a background thread. `tools` holds the
    server's tool schemas in the format `bind_tools` takes.

    Scripts the executor writes reach the same session through a local bridge: an HTTP endpoint on 127.0.0.1 with a
    random token, which bench/bench_tools.py calls. `script_env` holds the address and token for the script's process.
    """

    def __init__(
        self,
        pico_port: str | None = None,
        phyphox_url: str | None = None,
        server: Path = BENCH_SERVER,
        timeout: float = 30,
    ):
        # The server finds the Pico itself when no port is given (see bench/pico_connector.py).
        args = [str(server)]
        if pico_port:
            args += ["--port", pico_port]
        if phyphox_url:
            args += ["--phyphox-url", phyphox_url]
        self._server = StdioServerParameters(command=sys.executable, args=args)
        self._session: ClientSession | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._stop = asyncio.Event()
        self._ready: Future = Future()
        self._serving = asyncio.run_coroutine_threadsafe(self._serve(), self._loop)
        try:
            listed = self._ready.result(timeout=timeout)
        except BaseException:
            self.close()
            raise
        self.tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                },
            }
            for tool in listed
        ]
        self.names = [tool.name for tool in listed]
        self._script_calls: list[str] = []
        self._lock = threading.Lock()
        self._start_bridge()

    async def _serve(self) -> None:
        try:
            async with stdio_client(self._server) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set_result((await session.list_tools()).tools)
                    await self._stop.wait()
        except Exception as exc:
            if self._ready.done():
                raise
            self._ready.set_exception(exc)

    def call(self, name: str, args: dict, timeout: float = 60) -> str:
        if name not in self.names:
            return f"Error: there is no bench tool named {name!r}."
        future = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, args), self._loop)
        try:
            result = future.result(timeout=timeout)
        except KeyboardInterrupt:
            future.cancel()  # Ctrl+C cancels the step, so the call shouldn't go on in the background
            raise
        except Exception as exc:
            future.cancel()
            return f"Error: {exc or type(exc).__name__}"
        text = "\n".join(getattr(block, "text", str(block)) for block in result.content)
        return f"Error: {text}" if result.is_error else text

    def _start_bridge(self) -> None:
        bench, token = self, secrets.token_urlsafe(16)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("Authorization") != f"Bearer {token}":
                    self.send_error(403)
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    name, args = str(body["name"]), dict(body.get("args") or {})
                except (ValueError, KeyError, TypeError):
                    self.send_error(400)
                    return
                result = bench.call(name, args)
                with bench._lock:
                    bench._script_calls.append(f"{format_call({'name': name, 'args': args})} -> {result}")
                data = json.dumps({"result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._bridge = HTTPServer(("127.0.0.1", 0), Handler)
        self.script_env = {
            "BENCH_URL": f"http://127.0.0.1:{self._bridge.server_port}/call",
            "BENCH_TOKEN": token,
        }
        threading.Thread(target=self._bridge.serve_forever, daemon=True).start()

    def take_script_calls(self) -> list[str]:
        """The bench calls scripts made since the last take, as `call -> result` lines."""
        with self._lock:
            calls, self._script_calls = self._script_calls, []
        return calls

    def close(self) -> None:
        if getattr(self, "_bridge", None) is not None:
            self._bridge.shutdown()
            self._bridge.server_close()
        self._loop.call_soon_threadsafe(self._stop.set)
        try:
            self._serving.result(timeout=10)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def start_bench(pico_port: str | None, phyphox_url: str | None) -> Bench:
    try:
        with console.status(f"[{COLORS['hardware']}]Starting the light bench MCP server...[/]"):
            bench = Bench(pico_port, phyphox_url)
    except BaseException as exc:
        reason = (
            "the server exited during startup (its error is printed above)"
            if isinstance(exc, BaseExceptionGroup)
            else repr(exc)
        )
        console.print(
            Panel(
                f"Could not start {BENCH_SERVER.relative_to(PROJECT_DIR)}: {reason}\n"
                "Check that the Pico is plugged in (or pass --pico-port), and that phyphox is measuring on the "
                "phone with remote access on and `adb forward tcp:8080 tcp:8080` set up (or pass --phyphox-url). "
                "See docs/bench-setup.md.",
                title="[red]BENCH[/]",
                border_style="red",
            )
        )
        raise SystemExit(1)
    # Scripts import the stdlib-only helper from the working directory, where they run.
    shutil.copy2(BENCH_HELPER, RUN.work_dir / BENCH_HELPER.name)
    add_note(BENCH_NOTE, roles=("orchestrator",))
    add_note(BENCH_ROLE_NOTE, roles=("scientist", "general"))
    console.print(
        Panel(
            ", ".join(bench.names),
            title=f"[{COLORS['hardware']}]BENCH CONNECTED · executor tools[/]",
            border_style="green",
        )
    )
    return bench


def format_call(call: dict) -> str:
    args = ", ".join(f"{key}={value!r}" for key, value in call["args"].items())
    return f"{call['name']}({args})"
