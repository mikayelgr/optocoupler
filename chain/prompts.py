"""Role prompts from prompts/*.md, and the notes added to them at startup."""

from .config import PROMPTS_DIR

ROLE_PROMPTS = {
    "orchestrator": (PROMPTS_DIR / "orc.md").read_text(),
    "scientist": (PROMPTS_DIR / "sci.md").read_text(),
    "general": (PROMPTS_DIR / "gen.md").read_text(),
    "executor": (PROMPTS_DIR / "exe.md").read_text(),
}

HUMAN_NOTE = """

## Human Replies

The orchestrator can stop and ask the user a question. The question is in the orchestrator message (`question`),
the user's answer follows it as a message with role `human`, and a refused step is recorded in a `system` message.
A `human` message is the user speaking: when it picks a reading of the request, follow that reading. A step the
user refused did not run, so do not act as if it did.
"""

TASK_NOTE = """

## Task Files

The `task` messages at the start of the dialogue hold the text of the task files (homework sheets) the user's
request refers to, each headed by its file name. They are background: the request itself is the `user` message.
"""

DATA_NOTE = """

## Provided Data

`SNLS80mV.mat`, the Silverbox benchmark file named in Task 5, is already in the working directory, so no
web search or download is needed for it. Load it with `scipy.io.loadmat("SNLS80mV.mat")`: `V1` is the input
voltage u(t) and `V2` is the output voltage y(t), each a (1, 131072) float64 array, so take `["V1"][0]` and
`["V2"][0]`.
"""

BENCH_NOTE = """

## Light Bench

The executor can also drive a light bench: an LED it can dim from 0 to 100 percent, and a phone that measures the
light level in lux, through three tools: `set_led(percent)`, `led_off()` and `read_lux()`. It calls them directly, in
a loop within one turn, or from a script through the `bench_tools` module (useful for sweeps or many measurements).

- Send a bench request, such as reaching a target light level, straight to the executor. Its instruction says what to
  reach and which tools to use, for example: "Use led_off and read_lux to measure the room, then set_led and read_lux
  in small steps until the reading is about 19 lux."
- No other Python library or device file reaches the bench, and the scientist and the general agent have no tools
  for it. Do not ask them how to control the hardware. If you ask the executor for a script, say that it uses
  `bench_tools`.
- An executor turn on the bench changes real hardware, so it is a hard decision like any other executor turn: say in
  the question what the executor will set and measure.
"""

BENCH_ROLE_NOTE = """

## Light Bench

The executor controls a light bench through three tools: `set_led(percent)` (0 to 100), `led_off()` and `read_lux()`.
It calls them directly, or from a script with `import bench_tools` (`bench_tools.set_led(percent)`,
`bench_tools.led_off()`, `bench_tools.read_lux()`). No other Python library or device file reaches the bench, so never
name or invent one. When a plan or an answer involves the bench, describe it as calls to these tools.
"""


def add_note(note: str, roles=tuple(ROLE_PROMPTS)) -> None:
    for role in roles:
        ROLE_PROMPTS[role] += note


add_note(HUMAN_NOTE, roles=("scientist", "general", "executor"))
