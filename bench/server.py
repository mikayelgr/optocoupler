# MCP server: exposes the light bench as tools. Runs over stdio.
#   python bench/server.py [--port /dev/...] [--phyphox-url http://...]   (or `uv run python ...`)
# The pipeline starts it for you with --bench. To try it on its own in the MCP inspector:
#   mcp dev bench/server.py     (set PICO_PORT / PHYPHOX_URL to override the defaults)
import argparse

from mcp.server.mcpserver import MCPServer

import phone_connector
from pico_connector import Pico, resolve_port

mcp = MCPServer("light-bench")

_bench = {}  # "pico" and "url", set by connect()


def connect(port: str | None = None, phyphox_url: str | None = None) -> None:
    """Open the Pico's serial port and start the phyphox measurement. Fails fast if either is missing."""
    _bench["url"] = phone_connector.resolve_url(phyphox_url)
    _bench["pico"] = Pico(resolve_port(port))
    phone_connector.start(_bench["url"])


def _pico() -> Pico:
    if "pico" not in _bench:  # imported by `mcp dev` / `mcp run`, which skip __main__
        connect()
    return _bench["pico"]


@mcp.tool()
def set_led(percent: int) -> str:
    """Set the LED brightness. percent is an integer from 0 (off) to 100 (full)."""
    if not 0 <= percent <= 100:
        return "Error: percent must be between 0 and 100. Nothing was changed."
    reply = _pico().set_led(percent)
    return f"LED set to {percent}% (Pico: {reply})"


@mcp.tool()
def led_off() -> str:
    """Turn the LED off."""
    reply = _pico().off()
    return f"LED off (Pico: {reply})"


@mcp.tool()
def read_lux() -> str:
    """Read the current light level in lux from the phone sensor."""
    _pico()
    return f"{phone_connector.read_lux(_bench['url']):.1f} lux"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Light-bench MCP server (stdio).")
    parser.add_argument("--port", help="serial port of the Pico (default: $PICO_PORT, else auto-detect)")
    parser.add_argument(
        "--phyphox-url",
        help=f"phyphox address on the phone (default: $PHYPHOX_URL, else {phone_connector.DEFAULT_URL})",
    )
    args = parser.parse_args()
    connect(args.port, args.phyphox_url)
    mcp.run()
