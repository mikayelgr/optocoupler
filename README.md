# Photonics AI Solver Chain

A multi-agent [LangGraph](https://github.com/langchain-ai/langgraph) system that solves Task 5 of the PhotonicsAI homework: fitting a neural-network ODE model to the Silverbox oscillator and reporting a free-run test RMSE. Four roles share one dialogue: an orchestrator, a scientist, a general agent and an executor. All of them use models served through [OpenRouter](https://openrouter.ai/). The executor is the only role that writes and runs code, and the run ends with a Markdown report in `OUTPUT.md`. This is Task 6 of the same homework.

![Architecture: task.pdf enters the LangGraph loop as the first message; the orchestrator hands each turn to the scientist, general agent or executor, which append a reply and return the turn; only the executor uses tools; the run ends in the summarizer, which writes OUTPUT.md.](docs/architecture.svg)

*Every turn passes through the orchestrator. Each role reads the whole dialogue, appends one message and hands the turn back. Only the executor touches the outside world: web search, `uv add` and running scripts in the run's working directory. However the run ends, the summarizer writes `OUTPUT.md`.*

## Quick start

```sh
export OPENROUTER_API_KEY=...         # required
uv run python .                       # stop after 8 orchestrator decisions (Task 6)
uv run python . --max-decisions 0     # no limit: keep going until a verified test RMSE
```

Run the commands from the project root, where `task.pdf` and `SNLS80mV.mat` live. Press Ctrl+C at any time to stop. You still get a report of the work done so far.

## Requirements

| Requirement | Why |
| --- | --- |
| [uv](https://docs.astral.sh/uv/) | Runs the pipeline, and is also called by it: scripts run with `uv run`, and packages are added with `uv add` |
| Python 3.13 (`.python-version`, `requires-python >= 3.13`) | uv installs it if it's missing |
| `OPENROUTER_API_KEY` | Every model call, including web search, goes through OpenRouter |
| `task.pdf` in the current directory | The task text. If it's missing, a one-line Task 5 prompt is used instead |
| `SNLS80mV.mat` in the project root | The Silverbox data (from `SilverboxFiles.zip` on [nonlinearbenchmark.org](https://www.nonlinearbenchmark.org/)). If it's missing, the executor has to find the data on the web |

Python dependencies (`pyproject.toml`, locked in `uv.lock`):

| Package | Used by |
| --- | --- |
| `langgraph`, `langchain`, `langchain-openrouter` | The graph, messages and OpenRouter chat models |
| `pymupdf` | Extracting the text of `task.pdf` |
| `rich` | Live terminal panels and tables |
| `numpy`, `torch`, `scipy`, `h5py`, `requests`, `nonlinear-benchmarks` | Scripts the executor writes. Earlier runs added these with `uv add`; the pipeline itself doesn't import them |

## Capabilities

- **Structured routing.** The orchestrator returns a typed decision (`next_role`, `instruction`, `reason`) using JSON-schema structured output. It is told its decision number, the limit, and which files are in the working directory.
- **Shared dialogue state.** The state is a list of `{role, text}` messages. Each node returns only its new message, and LangGraph appends it.
- **Finish guard.** A `finish` is only accepted once the latest executor turn exited with code 0 and printed an RMSE value.
- **Decision limit.** The default is 8 decisions, as Task 6 requires. `--max-decisions 0` removes the limit.
- **Real code execution.** Every executor script is a new numbered file in a per-run working directory. It runs there with `uv run` in this project's environment, with a 5-minute timeout.
- **Dependency tool.** The executor can list PyPI packages. They're checked against a requirement pattern and installed with `uv add`.
- **Web search.** If data is missing, the executor can search through OpenRouter's web plugin and download what it finds into the working directory.
- **Data provisioning.** At startup, `SNLS80mV.mat` is checked for a real MAT-file header and copied into the working directory. Every role's prompt then says where it is and how to load it.
- **Resilience.** Every model call is retried indefinitely with exponential backoff, and a failing orchestrator model is recreated before retrying. The report is still written after the decision limit, Ctrl+C, or a graph error.
- **Live terminal UI.** Streamed output appears in `rich` panels, orchestrator decisions are shown as tables, and executor runs are marked OK or FAIL.

## How a run works

1. **Startup.** The script parses `--max-decisions` and creates a fresh working directory (`$TMPDIR/photonics_*`, which is kept after the run). It copies `SNLS80mV.mat` there and adds a "Provided Data" note to every role's prompt.
2. **Input.** The text of `task.pdf` becomes the first message, with role `user`.
3. **Routing.** The orchestrator reads the dialogue (trimmed to the last ~45k characters), its decision number and the working-directory listing. It then picks the next role and writes that role's instruction.
4. **Turns.** The scientist, general agent or executor reads its prompt from `prompts/` plus the full dialogue, appends one message and hands the turn back.
5. **End.** The run ends on an accepted `finish`, at the decision limit, or on Ctrl+C. The summarizer then turns the scientist and executor messages into a report with four sections: Task Summary, Methodology, Results and Conclusion. It's saved as `OUTPUT.md` in the current directory.

### One executor turn

![One executor turn: the prompt is built, the model replies; a search block triggers web_search_tool and the results go back into the prompt, up to 3 rounds; then listed packages are added with uv add, the script is run with uv run, and the result is appended as the executor's message.](docs/executor-turn.svg)

*The executor's model talks through fenced blocks. A ```` ```search ```` block (up to 3 queries) triggers a web search, and the results are added to the prompt for the next round. A ```` ```dependencies ```` block lists packages to install. A ```` ```python ```` block is the script. After 3 search rounds, the model is told to write the script. A failed `uv add` or a reply without a Python block is reported as a failure, and nothing is run.*

### How a run ends

![How a run ends: a finish decision is accepted only if the latest executor run printed an RMSE, otherwise the orchestrator is re-asked and then overridden to the executor; after each role turn the decision limit is checked; finish, the decision limit and Ctrl+C all lead to the summarizer and OUTPUT.md.](docs/termination.svg)

*An RMSE counts as "verified" when the most recent executor message shows `Exit code: 0` and its stdout contains "RMSE" followed by a number. If the orchestrator chooses `finish` without one, it's asked again with a rejection note, up to 2 times. If it still chooses `finish`, the turn goes to the executor. The decision limit is checked after each role's turn, so the last decision's role still runs.*

## Configuration

Command-line option:

| Flag | Default | Effect |
| --- | --- | --- |
| `--max-decisions N` | `8` | Maximum orchestrator decisions. `0` means no limit |

The other settings are constants in `__main__.py`:

| Setting | Default | Effect |
| --- | --- | --- |
| `WEB_SEARCH_ROUNDS` | `3` | Search rounds the executor may use per turn (up to 3 queries per round) |
| `FINISH_REJECTIONS` | `2` | Re-asks after an unjustified `finish` before the turn is sent to the executor |
| `code_tool(timeout=...)` | `300` s | Time limit for one executor script |
| `dependency_tool(timeout=...)` | `600` s | Time limit for one `uv add` |
| `plugins=[{"id": "web", "max_results": 5}]` | 5 results | Results per web search |
| `_trim_dialogue(max_chars=...)` | 45,000 chars | How much of the dialogue the orchestrator sees (oldest messages are dropped first) |
| `ORCHESTRATOR_MAX_CONTEXT` | 200,000 | `max_tokens` for the orchestrator model |
| `raw_findings[:30000]` | 30,000 chars | How much scientist/executor text the summarizer sees |
| `_workdir_listing(max_entries=...)` | 50 | Files listed from the working directory |
| `result[:1500]` | 1,500 chars | Search result text kept in the executor's message |
| `stream_with_retry(max_backoff=...)` | 300 s | Longest wait between retries (`2**attempt` seconds; attempts are unlimited) |
| `DATA_FILE` | `SNLS80mV.mat` | Data file copied into the working directory |
| `recursion_limit` | 10,000 | LangGraph's safety limit on graph steps |

Role behaviour lives in the prompts, which you can edit: `prompts/orc.md`, `prompts/sci.md`, `prompts/gen.md` and `prompts/exe.md`. The executor's reply format (the `search`, `dependencies` and `python` blocks) is in `EXECUTOR_CODE_FORMAT` in `__main__.py`.

## Models

Every call uses `google/gemma-4-31b-it` through OpenRouter. To change a model, edit the `model=` argument where that role's `ChatOpenRouter` is created.

| Role | Temperature | Call | Extras |
| --- | --- | --- | --- |
| orchestrator | 0.2 | single call | `with_structured_output(OrchestratorDecision, method="json_schema")`, `context-compression` plugin, `max_tokens=200000` |
| scientist | 0.01 | streamed | none |
| general | 0.01 | streamed | none |
| executor | 0.01 | streamed | none |
| web search (for the executor) | 0.01 | streamed | `web` plugin, 5 results (Exa-backed for this model, billed per request: [OpenRouter web search](https://openrouter.ai/docs/features/web-search)) |
| summarizer | 0.2 | streamed | none |

## Project layout

```
__main__.py        graph, nodes, tools and CLI
prompts/           role prompts: orc.md, sci.md, gen.md, exe.md
docs/              diagrams used in this README
task.pdf           homework text (input)
SNLS80mV.mat       Silverbox data (input; copied into each run's working directory)
OUTPUT.md          final report (generated, gitignored)
pyproject.toml     dependencies; uv.lock pins them
```

## Safety notes

- The executor runs code written by a model, as your user and with network access. The script file lives in a temp directory, but nothing else is sandboxed.
- `uv add` changes `pyproject.toml` and `uv.lock`, so check the diff after a run.
- Web searches cost OpenRouter credit on top of the model tokens.
