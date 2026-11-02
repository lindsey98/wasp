# WASP (AgentPI fork)

Fork of [facebookresearch/wasp](https://github.com/facebookresearch/wasp) — *WASP: Benchmarking Web Agent
Security Against Prompt Injection Attacks* ([arxiv:2504.18575](https://arxiv.org/abs/2504.18575)) — used as
the web-agent benchmark for the **AgentPI** online defense. Upstream is archived; this branch
(`agentpi-adapter`) carries the patches the adapter needs and is what the AgentPI CLI drives.

What changed from upstream (all in place, nothing to hand-edit):
- `visualwebarena/run_agentpi.py` + `OUTPUT_FORMAT=agentpi` — the step-2 agent is AgentPI's
  `OnlineWebAgent` (a `GPTWebAgent` subclass with the gate / sanitize / taint inserted).
- `evaluator_step_by_step.py` — a gate-refused action is recorded as `blocked`, not `compromised`;
  a refusal observation is neutralised before the next action is classified.
- Evaluators write `run_*.json` into the scenario's output dir (`WASP_RESULTS_DIR`), not `/tmp`.
- `environment_editors/` — external requests aborted, `load` not `networkidle`, idempotent account
  creation, GitLab's own error surfaced when issue creation fails.
- `tool_calling_agent.py` keeps the model's `reasoning_content` in the trace log.
- `scripts/restart_envs.sh` — one-shot pristine restart of GitLab + Reddit.

## Layout on the server

`AgentPI/` and `wasp/` **must be siblings** (`--wasp-root` defaults to `../wasp`). Only **GitLab + Reddit**
have injection tasks (skip shopping / wikipedia / maps / classifieds).

| piece | what it is | who runs in it |
|-------|-----------|----------------|
| **`agentpi` conda env** | AgentPI + deps | the **launcher** (`python -m src.agentpi.benchmarks.wasp.cli`) |
| `webarena_prompt_injections/venv` | step-1 injector + step-3 evaluators | shelled in by `scripts/run_end_to_end.sh` |
| `visualwebarena/venv` (py3.10) | VWA browser stack; **AgentPI installed here too** | the step-2 **agent** subprocess |
| Docker `gitlab` (:8023) + `forum` (:9999) | the two live target sites | — |
| a model endpoint | `OPENAI_BASE_URL` / `OPENAI_API_KEY` (+ optional Azure) | agent LLM + WASP's gpt-4o judge/injector |

You launch from the `agentpi` env; it shells into `run_end_to_end.sh`, which activates the WASP venvs itself
— never hand-activate them. Two gotchas that cause most of the pain:

- **`screen`/`tmux`** only for long *foreground* commands (`wget`, `docker load`, `setup.sh`, the run,
  `pytest`); `docker run -d` / `exec` / `ps` / `curl` return at once and survive a disconnect on their own.
- **`export`s are per-shell** — set the step-3 vars in the *same* window that launches the run.

## Setup (one-time)

**Prerequisites:** Docker; `python3.10` on PATH; ~80 GB free disk (Reddit ≈ 53 GB, GitLab tens of GB).

### 0 · Clone next to AgentPI  ·  dir: `<parent-of-AgentPI>`
```bash
git clone -b agentpi-adapter https://github.com/lindsey98/wasp.git
```

### 1 · Build WASP's venvs  ·  dir: `wasp/webarena_prompt_injections`  ·  screen (10–30 min)
Run with **no conda env active** so `setup.sh` builds the VWA venv on system python3.10, not conda's:
```bash
conda deactivate                       # repeat until the prompt has no (base)
cd wasp/webarena_prompt_injections && bash setup.sh   # builds ../visualwebarena/venv + ./venv, playwright install
export DATASET=webarena_prompt_injections
cd ../visualwebarena && venv/bin/python -c "import browser_env; print('import OK')"
```
`playwright install` may print OS packages to finish by hand.

### 2 · Stand up the two Docker sites  ·  dir: any
Load the images once. `wget` writes to cwd — `cd` to a big-disk dir first; `sudo docker images | grep -iE
"gitlab|postmill"` to skip a re-download.
```bash
# Reddit (postmill, ≈53 GB, resumable)
wget -c https://archive.org/download/postmill-populated-exposed-withimg/postmill-populated-exposed-withimg.tar
sudo docker load -i postmill-populated-exposed-withimg.tar

# GitLab (no reliable public .tar mirror right now; metis often down, not on archive.org)
wget -c http://metis.lti.cs.cmu.edu/webarena-images/gitlab-populated-final-port8023.tar
sudo docker load -i gitlab-populated-final-port8023.tar
```
Then create both containers with every required setting applied, in one command (also the way to **reset**
them before any later run — see below):
```bash
bash webarena_prompt_injections/scripts/restart_envs.sh          # gitlab + reddit; `gitlab` / `reddit` for one
# sanity: the registration form must render
curl -s http://localhost:8023/users/sign_up | grep -o new_user_first_name
```
GitLab boots slowly (3–5 min; the script polls instead of sleeping). The `/proc/sys/fs/file-max: Read-only
file system` log line is **harmless**. A plain reboot only needs `docker start gitlab forum`. Non-localhost
host: `GITLAB_HOST=<host> bash …/restart_envs.sh gitlab` also rewrites `external_url`.

The script recreates GitLab with `--shm-size=2g` and applies four settings (gravatar off, prometheus off in
*both* layers, signup on, PG/puma caps). Why each (all fix a failure the step-1 playwright setup hits in
practice):
- **`--shm-size=2g`** — Prometheus mmap metrics live in `/dev/shm`; Docker's default 64 MB is too small, so
  every *write* path 500s with `IOError (unmapped file)` while reads (`curl -I` → 302) still work.
- **Prometheus off in both layers** — `gitlab.rb` disables the monitoring stack, but the Rails app's own
  mmap metrics (`ApplicationSetting.prometheus_metrics_enabled`) are what actually 500.
- **gravatar off + signup on** — external gravatar hangs page `load` on a no-internet host; signup-off
  renders no `/users/sign_up` form (`#new_user_first_name` never appears).
- **`max_connections=500` + `puma worker_processes=2`** — omnibus auto-sizes puma workers to the CPU count,
  so a many-core box exhausts Postgres slots (`FATAL: remaining connection slots reserved`) → *intermittent*
  write 500s. The script does a full `gitlab-ctl restart` (not just puma) so Postgres picks up the limit.

The manual per-command version (and the other VWA sites, which you don't need) is in
[visualwebarena/environment_docker/README.md](visualwebarena/environment_docker/README.md).

### 3 · Point at the sites + keys  ·  set in the SAME shell as the run
```bash
export DATASET=webarena_prompt_injections
export GITLAB=http://localhost:8023    # must match gitlab's external_url exactly (host+port)
export REDDIT=http://localhost:9999
export OPENAI_BASE_URL=https://<endpoint>/v1 OPENAI_API_KEY=sk-...
```
**Endpoint gotcha:** `OPENAI_*` is shared by three callers — the step-2 agent, the step-1 injector, and the
step-3 evaluators — and the last two call **gpt-4o** (the judge reads `OPENAI_BASE_URL`). If your endpoint
also serves gpt-4o, one base_url covers all (verify: `curl -s $OPENAI_BASE_URL/models -H "Authorization:
Bearer $OPENAI_API_KEY" | grep gpt-4o`). If not — the common split is a gpt-4o gateway + a separate local
vLLM for the agent (the gateway has no channel for it → `503 No available channel`) — keep `OPENAI_BASE_URL`
on the gateway and give the agent its own endpoint, applied only to step 2:
```bash
export OPENAI_BASE_URL=https://<gpt-4o-gateway>/v1 OPENAI_API_KEY=<gateway-key>
export AGENTPI_AGENT_BASE_URL=http://localhost:8000/v1 AGENTPI_AGENT_API_KEY=EMPTY
```
(Or set `AZURE_API_ENDPOINT` / `AZURE_API_KEY` [+ optional `AZURE_API_VERSION`] for real Azure gpt-4o — the
injector/evaluator prefer Azure and then ignore `OPENAI_BASE_URL`, freeing it for the agent.) Getting this
wrong shows as the agent 503-ing (`No available channel for <agent model>`), or the agent running but
ASR/utility coming back all-0 (the gpt-4o judge was misrouted to an endpoint without gpt-4o).

### 4 · Install AgentPI into the VWA venv  ·  dir: `wasp/visualwebarena`
The step-2 subprocess imports `browser_env` *and* `agentpi`, so AgentPI must be importable **there**,
regardless of your conda env. Install editable so source edits take effect without reinstalling:
```bash
cd wasp/visualwebarena && source venv/bin/activate && uv pip install -e ../../AgentPI && deactivate
```

## Run  ·  dir: `AgentPI`  ·  `conda activate agentpi`  ·  screen (a run is long)

**Reset the sites first** (`bash ../wasp/webarena_prompt_injections/scripts/restart_envs.sh`). GitLab and
Reddit keep whatever earlier runs did — comments, deploy tokens, deleted projects, a public profile. The
attacker grader reads live state, so leftovers inflate attacker utility; a leftover comment identical to the
task text would be tainted by the guard. Step 4 of `run_end_to_end.sh` cleans up after a scenario, but a
crashed run skips it.

Re-set the step-3 exports if this is a fresh window (they are per-shell). The examples use
`local:Qwen3.6-35B-A3B` as the agent model (any `api:`/`local:` string works). Three knobs decide what a run
measures:

1. **Defense on/off** — `--use-original` runs the bare GPTWebAgent (no defense) baseline; omit it to run the
   AgentPI online defense.
2. **User task** — `--user-task`, the user-goal index. Given the injection, only **0** and **1** are
   supported (0 = comment, 1 = thumbs-up on GitLab / upvote on Reddit). Pass `0 1` to run both.
3. **Attack on/off** — `--run-attack`:
   - **off** → a benign run, measures **utility only** (WASP still plants the payload, but security is not
     reported — there is no true no-injection mode).
   - **on** → under attack, measures all three security numbers: **utility under attack**, **end-to-end
     ASR**, and **step-by-step ASR**.

`--injection-format` defaults to `goal_hijacking_plain_text`; `goal_hijacking_url_injection`,
`generic_plain_text`, `generic_url_injection` are also supported (pass several to sweep them).

```bash
# defense, under attack (default injection format) -- the main number
python -m src.agentpi.benchmarks.wasp.cli local:Qwen3.6-35B-A3B --q-model api:gpt-4o \
    --user-task 0 1 --run-attack --html

# no-defense baseline (ASR ceiling) to read the method against -- same tasks, defense off
python -m src.agentpi.benchmarks.wasp.cli local:Qwen3.6-35B-A3B --use-original \
    --user-task 0 1 --run-attack --html

# benign (utility only), no attack reported
python -m src.agentpi.benchmarks.wasp.cli local:Qwen3.6-35B-A3B --user-task 0 1

# a non-default injection format
python -m src.agentpi.benchmarks.wasp.cli local:Qwen3.6-35B-A3B --q-model api:gpt-4o \
    --user-task 0 --run-attack --injection-format generic_url_injection --html
```
Test one `--user-task` before a sweep. Re-running a scenario whose record already exists needs `-f`
(`--force-rerun`; per-task resume also skips tasks with a non-empty `agent_logs/<id>.jsonl`);
`--reauthor` discards the cached t=0 contract.

### Full sweep

WASP = **21 scenarios** (12 GitLab + 9 Reddit, one per `prompt_injections_setup_config` entry) × **2 user
goals** × **4 injection formats** = **168 end-to-end tasks**. One `(user_goal_idx, injection_format)`
invocation runs its 21 scenarios; the CLI loops over every `--user-task` × `--injection-format` pair, so the
full sweep is 8 invocations × ~15–20 min = hours. Run in `screen`.
```bash
# baseline (ASR ceiling)
python -m src.agentpi.benchmarks.wasp.cli local:Qwen3.6-35B-A3B --use-original --run-attack \
    --user-task 0 1 \
    --injection-format goal_hijacking_plain_text goal_hijacking_url_injection generic_plain_text generic_url_injection --html

# with the online defense (same 168 tasks)
python -m src.agentpi.benchmarks.wasp.cli local:Qwen3.6-35B-A3B --q-model api:gpt-4o --run-attack \
    --user-task 0 1 \
    --injection-format goal_hijacking_plain_text goal_hijacking_url_injection generic_plain_text generic_url_injection --html
```

## Output & scoring

Per scenario, `wasp/logs/<tag>/<user_task>_<injection_format>/` holds `agent_logs/<id>.jsonl` (one line per
step = the full message list), `guards/<id>.json` (contract, gate log, taint set, per-step record),
`agentpi_guard.json` (last task's dump), `traces/<id>.html` (with `--html`), and the three evaluator
outputs `run_step_by_step_asr.json`, `run_attacker_utility.json`, `run_user_utility.json`.

Records land under `AgentPI/runs_wasp/<model>[_defense_with_<guard>|_nodefense]/wasp/user_task_<n>/<attack>/…json`
in the shared `TaskRun` shape; `utility`/`security` are folded in from the `run_*.json`; `security=True`
means the injection succeeded.

**Print the four headline numbers** (benign utility, utility under attack, end-to-end ASR, step-by-step ASR)
from the records, aggregated over `--user-task` and `--injection-format` by summing the evaluators' raw
counts (dir: `AgentPI`):
```bash
python print_results_wasp.py --discover Qwen3.6-35B-A3B   # baseline vs defended, side by side
python print_results_wasp.py --name local_Qwen3.6-35B-A3B_nodefense   # one run
```

**Which metric:** the headline security number is **end-to-end ASR = attacker utility**
(`run_attacker_utility.json`, the attacker's *final* goal achieved, graded on live site state). **User
utility** (`run_user_utility.json`) is the functionality number (did the agent still do the user's task).
The **step-by-step ASR** (`run_step_by_step_asr.json`, LLM-judged, "compromised if any single action is")
is a looser diagnostic; it also reports `blocked_actions` (gate refusals) and `compromised_incl_blocked`
(what the number would be if the refusals had executed). WASP always plants the payload, so there is **no
clean/no-attack utility** here — measure the defense's utility tax on AgentDojo instead. A weak agent that
emits no tool call leaves an empty `agent_logs/<id>.jsonl` (evaluators skip it), so report the denominator
(classified vs. all 21).

**Sanity-check a run before trusting its numbers.** Attacker/user utility are graded on *live* state, so a
stale environment passes tasks the agent never touched, and a broken run (e.g. the agent stuck on
`about:blank`) looks "safe". Check that the `agent_logs` show real page URLs, that `guards/*.json` has
`allow` decisions (not only `block` with the same error), and that the attacker's URL never appears in the
action list of a task counted as passed.

### Re-run the evaluation only

Re-score existing agent logs without re-running the (expensive) agent. Requires GitLab/Reddit **still up and
un-cleaned** (the state still reflects the agent's actions), the step-3 env vars (gpt-4o `OPENAI_*`,
`NLTK_DATA`), and this branch's evaluator patches.
```bash
OUT=<wasp>/logs/<tag>/0_goal_hijacking_plain_text          # the scenario's output dir
export DATASET=webarena_prompt_injections GITLAB=http://localhost:8023 REDDIT=http://localhost:9999
export OPENAI_BASE_URL=https://<gpt-4o-gateway>/v1 OPENAI_API_KEY=<key> NLTK_DATA=/mnt/.../nltk_data

cd <wasp>/webarena_prompt_injections && source venv/bin/activate   # step-by-step ASR
python evaluator_step_by_step.py --log-folder $OUT/agent_logs/ --task-folder $OUT/webarena_tasks/ --format agentpi
deactivate
cd ../visualwebarena && source venv/bin/activate && bash prepare.sh   # attacker + user utility
python evaluator_final_step.py --log-folder $OUT/agent_logs/ --task-folder $OUT/webarena_tasks_attacker/ --format agentpi
python evaluator_final_step.py --log-folder $OUT/agent_logs/ --task-folder $OUT/webarena_tasks/ --format agentpi
deactivate
```
Each evaluator writes its `run_*.json` into `$OUT` (set `WASP_RESULTS_DIR` to send them elsewhere). This
does **not** backfill the `runs_wasp/.../*.json` record (only a full CLI `run_task` folds those in). If the
container was recreated since the logs were produced, the state no longer matches them → re-run the whole
scenario instead.

**Rebuild the control-flow HTML without re-running** (dir: `AgentPI`):
```bash
# all 21 tasks of a scenario -> traces/<task_id>.html + index.html
python -m src.agentpi.benchmarks.wasp.report_html_wasp --batch <wasp>/logs/<tag>/0_goal_hijacking_plain_text
# one trace from the single (last-task) dump
python -m src.agentpi.benchmarks.wasp.report_html_wasp <wasp>/logs/<tag>/0_goal_hijacking_plain_text/agentpi_guard.json
```

## Resetting environments

```bash
bash webarena_prompt_injections/scripts/restart_envs.sh            # gitlab + reddit
bash webarena_prompt_injections/scripts/restart_envs.sh gitlab     # or one of them
```
Recreates the container(s) from the pristine image, polls until each site answers, applies all GitLab
tuning in a single `reconfigure`, and prints the final HTTP status. `GITLAB_HOST=<host>` rewrites
`external_url`; `SKIP_TUNING=1` skips the gitlab.rb edits; `SUDO=""` forces no sudo.

## Upstream

Upstream WASP's own entrypoint (`webarena_prompt_injections/run.py`, `--output-format webarena|claude|
gpt_web_tools`) still works for the stock GPT/Claude agents; see the upstream README for those. Prompt
injection logic: [prompt_injector.py](webarena_prompt_injections/prompt_injector.py); attacker goals:
[experiment_config.raw.json](webarena_prompt_injections/configs/experiment_config.raw.json); payload
templates: [constants.py](webarena_prompt_injections/constants.py).

If you use this benchmark, cite the WASP paper (Evtimov, Zharmagambetov, Grattafiori, Guo, Chaudhuri —
[arxiv:2504.18575](https://arxiv.org/abs/2504.18575)).

## License

See [LICENSE](LICENSE) (upstream's license applies to this fork).
