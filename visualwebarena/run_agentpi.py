# The AgentPI online-method runner for WASP: the GPT tool-calling web agent (tool_calling_agent.GPTWebAgent)
# wrapped in AgentPI's contract/gate/reader, via agentpi.benchmarks.wasp.agent.OnlineWebAgent.
#
# Drop-in for tool_calling_agent.py in WASP step 2 (prompt_injector writes a run_agent.sh that calls this
# when --output-format agentpi). Same CLI as tool_calling_agent.py; the AgentPI-specific knobs arrive through
# AGENTPI_* env vars set by the AgentPI pipeline:
#   AGENTPI_AGENT_MODEL / AGENTPI_Q_MODEL / AGENTPI_C_MODEL / AGENTPI_J_MODEL  -- the four role models
#   AGENTPI_USE_ORIGINAL = 0|1   -- 1 runs the bare GPTWebAgent (no defense) baseline
#   AGENTPI_CONTRACT_FILE        -- cache path for the t=0 contract (omit to re-author)
#   AGENTPI_GUARD_DUMP           -- where to dump the gate trail + contract for the AgentPI record
#
# Requires agentpi importable in THIS (visualwebarena) venv:  uv pip install -e <path-to-AgentPI>
import json
import os
import subprocess
import tempfile

import click

from browser_env.auto_login import get_site_comb_from_filepath

from agentpi.core.online import Models
from agentpi.benchmarks.wasp.agent import OnlineWebAgent


def _models() -> Models:
    agent = os.environ.get("AGENTPI_AGENT_MODEL") or os.environ.get("MODEL", "gpt-4o")
    reader = os.environ.get("AGENTPI_Q_MODEL", agent)
    contract = os.environ.get("AGENTPI_C_MODEL", agent)
    judge = os.environ.get("AGENTPI_J_MODEL", agent)
    return Models(agent, reader, contract, judge)


def _dump_guard(agent: OnlineWebAgent) -> None:
    path = os.environ.get("AGENTPI_GUARD_DUMP")
    if not path:
        return
    g = getattr(agent, "guard", None)
    try:
        # viz_steps are captured in BOTH modes (guarded and --use-original baseline), so the HTML trace
        # exists even with no guard; the contract/gate fields are added only when a guard ran.
        payload = {"viz_steps": getattr(agent, "viz_steps", [])}
        if g is not None:
            payload.update({
                "query": g.query,
                "status": g.status,
                "objectives": [o.model_dump() for o in g.aset.objectives],
                "gate_log": g.gate_log,
                "steps": g.steps,
                "tainted": g.tainted,
            })
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 -- a dump failure must not fail the run; the scores still land
        print(f"run_agentpi: could not write guard dump ({e})")


@click.command()
@click.option("--webarena_config_path", type=str, help="path to the json config describing the task")
@click.option("--model", type=str, default="gpt-4o", help="The model backing the agent")
@click.option("--trace-log-filepath", type=str, default="/tmp/agentpi_web_loop_agent_logs.jsonl",
              help="Where to store the trajectories")
@click.option("--max_actions", type=int, default=None, help="The cap on actions by the agent")
@click.option("--max_observations_to_keep", type=int, default=3,
              help="The maximum number of past tool call results to keep")
def main(webarena_config_path, model, trace_log_filepath, max_actions, max_observations_to_keep):
    with open(webarena_config_path) as f:
        _c = json.load(f)
        start_url = _c["start_url"]
        user_objective = _c["intent"]
        if _c["storage_state"]:
            try:
                cookie_file_name = os.path.basename(_c["storage_state"])
                comb = get_site_comb_from_filepath(cookie_file_name)
                temp_dir = tempfile.mkdtemp()
                subprocess.run(["python", "browser_env/auto_login.py", "--auth_folder", temp_dir,
                                "--site_list", *comb])
                _c["storage_state"] = f"{temp_dir}/{cookie_file_name}"
                assert os.path.exists(_c["storage_state"])
                config_file = f"{temp_dir}/{os.path.basename(webarena_config_path)}"
                with open(config_file, "w") as fh:
                    json.dump(_c, fh)
            except Exception as e:
                print(f"Failed to automatically log in: {e}")
                print("Ignore this failure since agent has credentials in the system_prompt")

    use_original = os.environ.get("AGENTPI_USE_ORIGINAL", "0") == "1"
    contract_file = os.environ.get("AGENTPI_CONTRACT_FILE") or None
    max_actions = max_actions or int(os.environ.get("AGENTPI_MAX_STEPS", "20"))

    # Let the agent's action LLM use its OWN endpoint, separate from OPENAI_BASE_URL. WASP's step-1 injector
    # and step-3 evaluators call gpt-4o via OPENAI_BASE_URL (a hosted gateway); the agent model (e.g. a local
    # vLLM serving Qwen) usually lives elsewhere and that gateway has no channel for it. Each WASP step is a
    # separate process, so overriding OPENAI_BASE_URL here only affects this step-2 agent process.
    if os.environ.get("AGENTPI_AGENT_BASE_URL"):
        os.environ["OPENAI_BASE_URL"] = os.environ["AGENTPI_AGENT_BASE_URL"]
    if os.environ.get("AGENTPI_AGENT_API_KEY"):
        os.environ["OPENAI_API_KEY"] = os.environ["AGENTPI_AGENT_API_KEY"]
    # The agent's OWN action LLM is the REAL agent model (AGENTPI_AGENT_MODEL), bare served name -- NOT the
    # --model arg, which carries only the injector's Choice-valid placeholder (e.g. gpt-4o) and would
    # otherwise silently run the agent on the wrong model. Its OpenAI client routes via OPENAI_BASE_URL /
    # OPENAI_API_KEY, so point those at your vLLM for a local agent (local:/api: prefix is stripped here).
    agent_model = (os.environ.get("AGENTPI_AGENT_MODEL") or model).split(":", 1)[-1]
    # taint_source stays None by default: sanitize (reported speech) + objective-admission are the active
    # defenses; a precise injected-span detector can be wired here later (the plan's tunable knob).
    with OnlineWebAgent(agent_model, trace_log_filepath, _models(), contract_file=contract_file,
                        use_original=use_original, taint_source=None) as agent:
        agent.loop(start_url=start_url, user_objective=user_objective, max_actions=max_actions,
                   max_observations_to_keep=max_observations_to_keep)
        _dump_guard(agent)


if __name__ == "__main__":
    main()
