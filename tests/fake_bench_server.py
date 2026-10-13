# A stand-in for bench/server.py with the same tools and no hardware: lux = 20 (room light) + 3 per LED percent.
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("fake-light-bench")
led = {"percent": 0}


@mcp.tool()
def set_led(percent: int) -> str:
    """Set the LED brightness. percent is an integer from 0 (off) to 100 (full)."""
    if not 0 <= percent <= 100:
        return "Error: percent must be between 0 and 100. Nothing was changed."
    led["percent"] = percent
    return f"LED set to {percent}% (Pico: OK {percent})"


@mcp.tool()
def led_off() -> str:
    """Turn the LED off."""
    led["percent"] = 0
    return "LED off (Pico: OK 0)"


@mcp.tool()
def read_lux() -> str:
    """Read the current light level in lux from the phone sensor."""
    return f"{20 + 3 * led['percent']:.1f} lux"


if __name__ == "__main__":
    mcp.run()
