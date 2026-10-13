"""Offline tests: scripted models instead of OpenRouter, and a stand-in bench server instead of the hardware."""

from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from chain import config, llm
from chain.bench import Bench
from chain.graph import build_graph, run_thread
from chain.scope import drop_bench, has_required_result, scope_request
from chain.state import DialogueState, HumanVerdict, OrchestratorDecision, RequestScope

FAKE_SERVER = Path(__file__).parent / "fake_bench_server.py"

NONE = RequestScope(tasks=[], result="none", quantity="", reason="")
RMSE = RequestScope(tasks=["task1.pdf"], result="script", quantity="RMSE", reason="")
LUX = RequestScope(tasks=[], result="bench", quantity="lux", reason="")


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def decision(role, **fields):
    base = dict(next_role=role, stop_kind="none", pending_step="none", instruction="", question="", reason="r")
    return OrchestratorDecision(**(base | fields))


@pytest.fixture(autouse=True)
def run_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(config.RUN, "work_dir", tmp_path)
    monkeypatch.setattr(config.RUN, "max_decisions", 8)
    monkeypatch.setattr(config.RUN, "bench", None)
    monkeypatch.setattr(config.console, "quiet", True)


@pytest.fixture
def bench():
    bench = Bench(server=FAKE_SERVER)
    yield bench
    bench.close()


def run(monkeypatch, scope, decisions, replies=(), answers=()):
    """Run one thread with scripted structured decisions, streamed replies and human answers."""
    decisions, answers = iter(decisions), iter(answers)
    prompts = []

    def decide(prompt, schema, status=""):
        prompts.append(prompt)
        return next(decisions)

    monkeypatch.setattr(llm, "decide", decide)
    model = ScriptedModel(responses=list(replies))
    monkeypatch.setattr(llm, "chat_model", lambda **kwargs: model)
    graph = build_graph()
    thread = {"configurable": {"thread_id": "test"}, "recursion_limit": 100}
    state = DialogueState(messages=[{"role": "user", "text": "request"}], scope=scope.model_dump())
    run_thread(graph, state, thread, ask=lambda payload: next(answers))
    values = graph.get_state(thread).values
    return values["messages"], RequestScope.model_validate(values["scope"]), prompts


def roles(messages):
    return [msg["role"] for msg in messages]


def test_bench_client_calls_tools_in_order(bench):
    assert bench.names == ["set_led", "led_off", "read_lux"]
    assert bench.call("set_led", {"percent": 10}) == "LED set to 10% (Pico: OK 10)"
    assert bench.call("read_lux", {}) == "50.0 lux"
    assert bench.call("set_led", {"percent": 150}).startswith("Error: percent must be")
    assert bench.call("set_led", {"percent": "abc"}).startswith("Error:")
    assert bench.call("nope", {}) == "Error: there is no bench tool named 'nope'."


def test_bench_turn_loops_through_hardware_until_a_reading(monkeypatch, bench):
    monkeypatch.setattr(config.RUN, "bench", bench)
    replies = [
        AIMessage("", tool_calls=[{"name": "set_led", "args": {"percent": 50}, "id": "1"},
                                  {"name": "read_lux", "args": {}, "id": "2"}]),
        AIMessage("", tool_calls=[{"name": "set_led", "args": {"percent": 60}, "id": "3"},
                                  {"name": "read_lux", "args": {}, "id": "4"}]),
        AIMessage("LED at 60%, measured 200.0 lux."),
    ]
    messages, _, _ = run(monkeypatch, LUX, [decision("executor"), decision("finish")], replies)
    assert roles(messages) == ["user", "orchestrator", "executor", "hardware", "executor", "hardware",
                               "executor", "orchestrator"]
    assert messages[5]["text"].splitlines() == ["set_led(percent=60) -> LED set to 60% (Pico: OK 60)",
                                                "read_lux() -> 200.0 lux"]
    assert has_required_result(messages, LUX)


def test_bench_reading_must_follow_the_last_change():
    changed = [{"role": "hardware", "text": "read_lux() -> 20.0 lux\nset_led(percent=5) -> LED set to 5%"},
               {"role": "executor", "text": "Bench report: done"}]
    assert not has_required_result(changed, LUX)


def test_request_without_measurement_finishes_after_an_answer(monkeypatch):
    messages, _, prompts = run(monkeypatch, NONE, [decision("general"), decision("finish")],
                               [AIMessage("Lux is lumens per square metre.")])
    assert roles(messages) == ["user", "orchestrator", "general", "orchestrator"]
    assert "does not ask for a measured value" in prompts[0]


def test_finish_without_the_required_value_becomes_a_hard_decision(monkeypatch):
    monkeypatch.setattr(config.RUN, "max_decisions", 1)  # end the run after the refusal
    messages, _, prompts = run(monkeypatch, RMSE, [decision("finish")] * 3 + [HumanVerdict(approved=False, reason="")],
                               answers=["no"])
    assert roles(messages) == ["user", "orchestrator", "human", "system"]
    fallback = OrchestratorDecision.model_validate_json(messages[1]["text"])
    assert fallback.is_hard_decision and fallback.pending_step == "executor" and "RMSE" in fallback.instruction
    assert "value labelled RMSE" in prompts[1]


def test_printed_value_follows_the_scoped_quantity():
    run_ok = [{"role": "executor", "text": "Exit code: 0\nStdout: accuracy: 0.93\nStderr: \nStatus: success"}]
    accuracy = RequestScope(tasks=[], result="script", quantity="accuracy", reason="")
    assert has_required_result(run_ok, accuracy)
    assert not has_required_result(run_ok, RMSE)


def test_answering_an_ambiguous_query_rescopes_the_request(monkeypatch):
    explain = RequestScope(tasks=["task1.pdf"], result="none", quantity="", reason="the user chose to explain")
    messages, scope, prompts = run(
        monkeypatch, RMSE,
        [decision("human", stop_kind="ambiguous_query", question="1. measure it  2. explain it?"), explain,
         decision("general"), decision("finish")],
        [AIMessage("It measures the free-run error.")],
        answers=["2"],
    )
    assert scope.result == "none" and roles(messages)[-1] == "orchestrator"
    assert "1. measure it" in prompts[1] and "User's answer: 2" in prompts[1]


def test_scope_overrides(monkeypatch):
    tasks = {"task1.pdf": "Homework 1", "task3.pdf": "Homework 3"}

    def scoped(**fields):
        monkeypatch.setattr(llm, "decide", lambda *a, **k: RequestScope(**(dict(reason="") | fields)))

    scoped(tasks=["task3.pdf"], result="bench", quantity="lux")
    scope = scope_request("Make it 200 lux.", tasks, fixed_tasks=[])
    assert scope.tasks == [] and scope.result == "bench"  # --task none wins; a bench request is kept without a bench
    assert drop_bench(scope).result == "none"

    scoped(tasks=["task3.pdf", "task9.pdf"], result="none", quantity="")
    assert scope_request("Explain Homework 3.", tasks).tasks == ["task3.pdf"]  # unknown names are dropped

    scoped(tasks=[], result="none", quantity="")
    assert scope_request("Do Task 5.", tasks, fixed_tasks=["task1.pdf"]).tasks == ["task1.pdf"]  # --task task1

    scoped(tasks=[], result="script", quantity="")
    assert scope_request("Compute it.", {}).quantity == "result"  # a blank label gets a default


def test_request_prompt_asks_again_until_something_is_typed(monkeypatch, bench):
    from chain import cli

    monkeypatch.setattr(config.RUN, "bench", bench)
    replies = iter(["", "   ", "Make it 200 lux."])
    monkeypatch.setattr(config.console, "input", lambda prompt: next(replies))
    assert cli.ask_request() == "Make it 200 lux."


def test_executor_sees_the_bench_tools_before_the_script_format(monkeypatch, bench):
    from chain.executor import executor_node

    monkeypatch.setattr(config.RUN, "bench", bench)
    prompts = []

    def stream(model, prompt, label):
        prompts.append(prompt)
        return "Done.", AIMessage("Done.")

    monkeypatch.setattr(llm, "stream_with_retry", stream)
    monkeypatch.setattr(llm, "chat_model", lambda **kwargs: ScriptedModel(responses=[]))
    executor_node(DialogueState(messages=[{"role": "user", "text": "Make it 19 lux."}], scope=LUX.model_dump()))
    prompt = prompts[0]
    assert prompt.index("## Light Bench") < prompt.index("## Response Format For This Turn")
    assert "never guess a library name" in prompt and "import bench_tools" in prompt
    assert "When the step needs a script (for computation, or for many bench measurements), reply with exactly one" in prompt


def test_bench_request_without_bench_offers_to_connect(monkeypatch, bench):
    from chain import cli, prompts

    for role in prompts.ROLE_PROMPTS:
        monkeypatch.setitem(prompts.ROLE_PROMPTS, role, prompts.ROLE_PROMPTS[role])  # restored after the test
    monkeypatch.setattr("chain.bench.Bench", lambda pico_port, phyphox_url: bench)

    monkeypatch.setattr(config.console, "input", lambda prompt: "n")
    assert not cli.offer_bench(None, None) and config.RUN.bench is None

    monkeypatch.setattr(config.console, "input", lambda prompt: "")
    assert cli.offer_bench(None, None) and config.RUN.bench is bench
    for role in ("orchestrator", "scientist", "general"):
        assert "set_led(percent)" in prompts.ROLE_PROMPTS[role]


def test_bench_request_must_open_with_a_tool_call(monkeypatch, bench):
    from chain.executor import executor_node

    monkeypatch.setattr(config.RUN, "bench", bench)
    choices = []

    class Recording(ScriptedModel):
        def bind_tools(self, tools, **kwargs):
            choices.append(kwargs.get("tool_choice"))
            return self

    monkeypatch.setattr(llm, "chat_model", lambda **kwargs: Recording(responses=[AIMessage("Done.")] * 2))
    first = [{"role": "user", "text": "Make it 19 lux."}]
    executor_node(DialogueState(messages=first, scope=LUX.model_dump()))
    later = first + [{"role": "executor", "text": "Bench calls: read_lux()"}, {"role": "hardware", "text": "read_lux() -> 8.0 lux"}]
    executor_node(DialogueState(messages=later, scope=LUX.model_dump()))
    assert choices == ["required", None]  # forced only before the turn's first hardware call


SCRIPT = """
import bench_tools
print(bench_tools.led_off())
print(bench_tools.set_led(10))
print("lux:", bench_tools.read_lux())
try:
    bench_tools.set_led(150)
except RuntimeError as exc:
    print("rejected:", exc)
"""


def test_scripts_reach_the_bench_through_bench_tools(monkeypatch, tmp_path, bench):
    import shutil
    import urllib.error
    import urllib.request

    from chain.tools import code_tool

    monkeypatch.setattr(config.RUN, "bench", bench)
    shutil.copy2(config.BENCH_HELPER, tmp_path / "bench_tools.py")
    exit_code, stdout, stderr = code_tool(SCRIPT)
    assert exit_code == 0, stderr
    assert stdout.splitlines() == [
        "LED off (Pico: OK 0)",
        "LED set to 10% (Pico: OK 10)",
        "lux: 50.0",
        "rejected: Error: percent must be between 0 and 100. Nothing was changed.",
    ]
    assert bench.take_script_calls() == [
        "led_off() -> LED off (Pico: OK 0)",
        "set_led(percent=10) -> LED set to 10% (Pico: OK 10)",
        "read_lux() -> 50.0 lux",
        "set_led(percent=150) -> Error: percent must be between 0 and 100. Nothing was changed.",
    ]
    assert bench.take_script_calls() == []

    wrong = urllib.request.Request(bench.script_env["BENCH_URL"], data=b"{}", headers={"Authorization": "Bearer no"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(wrong, timeout=5)
    assert error.value.code == 403


def test_bench_tools_without_a_bench_says_so(monkeypatch):
    import importlib.util

    monkeypatch.delenv("BENCH_URL", raising=False)
    spec = importlib.util.spec_from_file_location("bench_tools", config.BENCH_HELPER)
    bench_tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench_tools)
    with pytest.raises(RuntimeError, match="not connected"):
        bench_tools.read_lux()


def test_a_scripts_bench_calls_go_into_the_dialogue(monkeypatch, tmp_path, bench):
    import shutil

    from chain.executor import executor_node

    monkeypatch.setattr(config.RUN, "bench", bench)
    shutil.copy2(config.BENCH_HELPER, tmp_path / "bench_tools.py")
    sweep = "```python\nimport bench_tools\nbench_tools.set_led(5)\nprint('lux:', bench_tools.read_lux())\n```"
    monkeypatch.setattr(llm, "chat_model", lambda **kwargs: ScriptedModel(responses=[AIMessage(sweep)]))
    result = executor_node(DialogueState(messages=[{"role": "user", "text": "Sweep it."}], scope=LUX.model_dump()))
    executor, hardware = result["messages"]
    assert "lux: 35.0" in executor["text"]
    assert hardware == {
        "role": "hardware",
        "text": "Bench calls made by the script:\nset_led(percent=5) -> LED set to 5% (Pico: OK 5)\nread_lux() -> 35.0 lux",
    }
    assert has_required_result(result["messages"], LUX)


def cancel_first_reply(monkeypatch):
    """Ctrl+C during the first streamed reply."""
    stream, cancels = llm.stream_with_retry, iter([True])

    def cancelled(model, prompt, label):
        if next(cancels, False):
            raise KeyboardInterrupt
        return stream(model, prompt, label)

    monkeypatch.setattr(llm, "stream_with_retry", cancelled)


def test_ctrl_c_cancels_the_step_and_a_note_steers_the_team(monkeypatch):
    cancel_first_reply(monkeypatch)
    messages, scope, prompts = run(
        monkeypatch, NONE, [decision("scientist"), NONE, decision("general"), decision("finish")],
        [AIMessage("Lux is lumens per square metre.")],
        answers=["Skip the plan, just explain it."],
    )
    assert roles(messages) == ["user", "orchestrator", "system", "human", "orchestrator", "general", "orchestrator"]
    assert messages[2]["text"].startswith("The user cancelled the scientist's reply")
    assert "User's note after cancelling a step: Skip the plan" in prompts[1]
    assert "Skip the plan" in prompts[2] and scope == NONE


def test_a_cancelled_scripts_bench_calls_go_into_the_dialogue(monkeypatch, tmp_path, bench):
    import shutil

    from chain import executor, tools

    def cancelled_script(code):
        tools.code_tool(code)
        raise KeyboardInterrupt  # Ctrl+C just as the script ends

    monkeypatch.setattr(config.RUN, "bench", bench)
    monkeypatch.setattr(executor, "code_tool", cancelled_script)
    shutil.copy2(config.BENCH_HELPER, tmp_path / "bench_tools.py")
    sweep = "```python\nimport bench_tools\nbench_tools.set_led(5)\n```"
    messages, _, _ = run(monkeypatch, NONE, [decision("executor"), NONE, decision("finish")], [AIMessage(sweep)],
                         answers=["Leave it at 5%."])
    assert roles(messages) == ["user", "orchestrator", "hardware", "system", "human", "orchestrator"]
    assert messages[2]["text"].endswith("set_led(percent=5) -> LED set to 5% (Pico: OK 5)")
    assert "bench settings it already made stay as they are" in messages[3]["text"]


def test_ctrl_c_at_the_decision_limit_stops_the_run(monkeypatch):
    monkeypatch.setattr(config.RUN, "max_decisions", 1)
    cancel_first_reply(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        run(monkeypatch, NONE, [decision("scientist")])


def test_no_bench_call_waits(monkeypatch):
    import sys
    import time

    monkeypatch.syspath_prepend(str(config.PROJECT_DIR / "bench"))
    sys.modules.pop("server", None)
    import server

    class FakePico:
        def set_led(self, percent):
            return f"OK {percent}"

        def off(self):
            return "OK 0"

    monkeypatch.setattr(server, "_bench", {"pico": FakePico(), "url": "http://phone"})
    monkeypatch.setattr(server.phone_connector, "read_lux", lambda url: 42.0)
    start = time.monotonic()
    for percent in (100, 0, 100):
        assert server.set_led(percent) == f"LED set to {percent}% (Pico: OK {percent})"
    assert server.led_off() == "LED off (Pico: OK 0)"
    assert server.read_lux() == "42.0 lux"
    assert time.monotonic() - start < 0.1  # timing is left to the caller, so a pulse pattern keeps its shape
