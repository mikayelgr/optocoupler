"""Light-bench tools for scripts the executor writes. Standard library only.

With --bench, the pipeline copies this file into the run's working directory, so a script can use it directly:

    import bench_tools
    bench_tools.led_off()              # "LED off (Pico: OK 0)"
    bench_tools.set_led(40)            # "LED set to 40% (Pico: OK 40)"
    lux = bench_tools.read_lux()       # 19.0

Each call goes through the pipeline's MCP session to bench/server.py: the same tools the executor calls directly.
The pipeline passes the address and the token in BENCH_URL and BENCH_TOKEN.

No call waits: set_led and led_off return in a few milliseconds, so a script can time them to pulse the LED, and
read_lux returns the sensor's latest value at once.
"""

import json
import os
import urllib.request


def call(name: str, **args) -> str:
    """Call one bench tool and return its text result; raises RuntimeError if the tool reports an error."""
    url, token = os.environ.get("BENCH_URL"), os.environ.get("BENCH_TOKEN")
    if not url or not token:
        raise RuntimeError("The light bench is not connected in this run (start the pipeline with --bench).")
    request = urllib.request.Request(
        url,
        data=json.dumps({"name": name, "args": args}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.load(response)["result"]
    if result.startswith("Error"):
        raise RuntimeError(result)
    return result


def set_led(percent: int) -> str:
    """Set the LED brightness, an integer from 0 (off) to 100 (full)."""
    return call("set_led", percent=int(percent))


def led_off() -> str:
    """Turn the LED off."""
    return call("led_off")


def read_lux() -> float:
    """Read the current light level in lux."""
    return float(call("read_lux").split()[0])
