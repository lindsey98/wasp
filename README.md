# WASP: Benchmarking Web Agent Security Against Prompt Injection Attacks

Implementation of **WASP** ([arxiv:2504.18575](https://arxiv.org/abs/2504.18575)): an end-to-end executable
web environment (self-hosted GitLab + Reddit) in which an attacker plants prompt injections and a web agent
is measured on whether it still does the user's task, whether it ends up doing the attacker's, and at which
step it went wrong. Upstream repo: [facebookresearch/wasp](https://github.com/facebookresearch/wasp).

Pipeline (`webarena_prompt_injections/scripts/run_end_to_end.sh`), per `(user goal, injection format)`:

1. **inject** — `prompt_injector.py` drives a Playwright browser to plant the payload in GitLab/Reddit and
   writes the task files + a generated `run_agent.sh`;
2. **agent** — the web agent runs the 21 scenarios, logging one `agent_logs/<id>.jsonl` each;
3. **evaluate** — `evaluator_step_by_step.py` (LLM-judged, per action) and `evaluator_final_step.py`
   (attacker goal and user goal, graded on live site state) write `run_*.json`;
4. **cleanup** — `environment_cleanup.py` removes what the attack/agent created.

## Layout on the server

WASP needs a live browser stack, not just a pip install — do **everything on the server**. Only **GitLab +
Reddit** have injection tasks (skip shopping / wikipedia / maps / classifieds).

| piece | what it is | who runs in it |
|-------|-----------|----------------|
| `webarena_prompt_injections/venv` | step-1 injector + step-3 evaluators + `run.py` | shelled in by `run_end_to_end.sh` |
| `visualwebarena/venv` (py3.10) | VWA browser stack | the step-2 **agent** subprocess |
| Docker `gitlab` (:8023) + `forum` (:9999) | the two live target sites | — |
| a model endpoint | `OPENAI_BASE_URL` / `OPENAI_API_KEY` (+ optional Azure) | agent LLM + the gpt-4o judge/injector |

`run_end_to_end.sh` activates the venvs itself — never hand-activate them. Two gotchas that cause most of
the pain:

- **`screen`/`tmux`** only for long *foreground* commands (`wget`, `docker load`, `setup.sh`, the run,
  `pytest`); `docker run -d` / `exec` / `ps` / `curl` return at once and survive a disconnect on their own.
- **`export`s are per-shell** — set the step-3 vars in the *same* window that launches the run.

## Setup (one-time)

**Prerequisites:** Docker; `python3.10` on PATH; ~80 GB free disk (Reddit ≈ 53 GB, GitLab tens of GB).

### 1 · Build the venvs  ·  dir: `webarena_prompt_injections`  ·  screen (10–30 min)
Run with **no conda env active** so `setup.sh` builds the VWA venv on system python3.10, not conda's:
```bash
conda deactivate                       # repeat until the prompt has no (base)
cd webarena_prompt_injections && bash setup.sh   # builds ../visualwebarena/venv + ./venv, playwright install
export DATASET=webarena_prompt_injections
cd ../visualwebarena && venv/bin/python -c "import browser_env; print('import OK')"
```
`playwright install` may print OS packages to finish by hand. Optional check that VWA is intact:
`cd visualwebarena && pytest -x`.

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

The environment editors in `webarena_prompt_injections/environment_editors/` handle the browser side —
external requests aborted so `load` never waits on gravatar/CDN assets, `load` not `networkidle`,
screenshots that wait and never crash a run, idempotent account creation, GitLab's own error surfaced when
issue creation fails.

The manual per-command version (and the other VWA sites, which you don't need) is in
[visualwebarena/environment_docker/README.md](visualwebarena/environment_docker/README.md).

### 3 · Point at the sites + keys  ·  set in the SAME shell as the run
```bash
export DATASET=webarena_prompt_injections
export GITLAB=http://localhost:8023    # must match gitlab's external_url exactly (host+port)
export REDDIT=http://localhost:9999
export OPENAI_BASE_URL=https://<endpoint>/v1 OPENAI_API_KEY=sk-...
```
Azure instead of OpenAI:
```bash
export AZURE_API_KEY='your_azure_api_key'
export AZURE_API_ENDPOINT='your_azure_endpoint'
export AZURE_API_VERSION='2024-10-21'
```
For Claude, the code expects `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN`, plus
the full AWS Bedrock model name in `webarena_prompt_injections/system_prompts/claude_*_agent.json`.
OpenAI/Azure keys are still needed for the evaluators.

**Endpoint gotcha:** `OPENAI_*` is shared by three callers — the step-2 agent, the step-1 injector, and the
step-3 evaluators — and the last two call **gpt-4o** (the judge reads `OPENAI_BASE_URL`). If your endpoint
also serves gpt-4o, one base_url covers all (verify: `curl -s $OPENAI_BASE_URL/models -H "Authorization:
Bearer $OPENAI_API_KEY" | grep gpt-4o`). If not — the common split is a gpt-4o gateway + a separate local
vLLM for the agent (the gateway has no channel for it → `503 No available channel`) — keep `OPENAI_BASE_URL`
on the gateway and point the agent at its own endpoint. Setting `AZURE_API_ENDPOINT` / `AZURE_API_KEY` for
real Azure gpt-4o also works: the injector/evaluator prefer Azure and then ignore `OPENAI_BASE_URL`,
freeing it for the agent. Getting this wrong shows as the agent 503-ing (`No available channel for <agent
model>`), or the agent running but ASR/utility coming back all-0 (the gpt-4o judge was misrouted to an
endpoint without gpt-4o).

## Run  ·  dir: `webarena_prompt_injections`  ·  screen (a run is long)

**Reset the sites first** (`bash scripts/restart_envs.sh`). GitLab and Reddit keep whatever earlier runs
did — comments, deploy tokens, deleted projects, a public profile. The attacker grader reads live state, so
leftovers inflate attacker utility. Step 4 cleans up after a scenario, but a crashed run skips it.

Re-set the exports if this is a fresh window (they are per-shell). The main entrypoint is `run.py`; it
writes the per-scenario configs and shells into `run_end_to_end.sh` for every `(user goal, injection
format)` pair:
```bash
python run.py --config configs/experiment_config.raw.json \
              --model gpt-4o \
              --system-prompt configs/system_prompts/wa_p_som_cot_id_actree_3s.json \
              --output-dir ../logs/<run-name> \
              --output-format webarena
```
- `--config` — attacker goals and how each is graded.
- `--model` — backbone LLM: `gpt-4o`, `gpt-4o-mini`, `claude-35`, `claude-37`.
- `--system-prompt` — the scaffolding's system prompt (`configs/system_prompts/wa_*` for (Visual)WebArena,
  `claude_*` for Claude; a `*_defense.json` variant adds defensive instructions).
- `--output-dir` — logs and traces; one sub-directory per `<user_goal>_<injection_format>`.
- `--output-format` — which agent runner `run_agent.sh` is generated for: `webarena`, `claude`,
  `gpt_web_tools` (observation under the `tool` role, for testing instruction-hierarchy models).
- `--user_goal_start` / `--run-single` — restrict to a subset of user goals or one
  `(user goal, injection format)` pair; a full run is 4–6 hours.

A run is **21 scenarios** (12 GitLab + 9 Reddit, one per `prompt_injections_setup_config` entry) × **2 user
goals** (0 = comment, 1 = thumbs-up / upvote) × **4 injection formats** (`goal_hijacking_plain_text`,
`goal_hijacking_url_injection`, `generic_plain_text`, `generic_url_injection`) = **168 end-to-end tasks**.
Test one pair with `--run-single` before a sweep.

Other agents:
```bash
# Claude 3.5 Sonnet computer-use agent
python run.py --model claude-35 --system-prompt configs/system_prompts/claude_35_agent.json \
              --output-dir ../logs/<run-name> --output-format claude
# GPT-4o-mini with tool/function calling
python run.py --model gpt-4o-mini --output-dir ../logs/<run-name> --output-format gpt_web_tools
```

## Output & scoring

Per scenario, `<output-dir>/<user_goal>_<injection_format>/` holds:

| file | what |
|------|------|
| `agent_logs/<id>.jsonl` | the agent's trace, one line per step (the full message list; the model's reasoning is kept) |
| `webarena_tasks/`, `webarena_tasks_attacker/` | the user-goal and attacker-goal task specs the graders read |
| `instantiated_prompt_injections_config.json`, `run_agent.sh` | what was injected, how the agent was launched |
| `run_step_by_step_asr.json` | per-action classification: `not_compromised`, `compromised`, `outright_refusal`, `blocked_actions`, `compromised_incl_blocked` |
| `run_attacker_utility.json` | scenarios where the attacker's goal was achieved (= **end-to-end ASR**) |
| `run_user_utility.json` | scenarios where the user's goal was achieved (= **utility under attack**) |

The evaluators write their `run_*.json` into the scenario directory (set `WASP_RESULTS_DIR` to send them
elsewhere), so later invocations never clobber each other.

**Which metric:** the headline security number is **end-to-end ASR = attacker utility** — the attacker's
*final* goal achieved, graded on live site state (`program_html` checks and `exfil` URL checks). **User
utility** is the functionality number. The **step-by-step ASR** (LLM-judged, "compromised if any single
action is") is a looser diagnostic: an action the agent's own policy refused is counted as `blocked`, not
`compromised`, and `compromised_incl_blocked` says what the number would be had those refusals executed.
WASP always plants the payload, so there is **no clean/no-attack utility** here. A weak agent that emits no
tool call leaves an empty `agent_logs/<id>.jsonl` (evaluators skip it), so report the denominator
(classified vs. all 21).

**Sanity-check a run before trusting its numbers.** Attacker/user utility are graded on *live* state, so a
stale environment passes tasks the agent never touched, and a broken run (e.g. the agent stuck on
`about:blank` because the first `goto` failed) looks "safe". Check that `agent_logs` show real page URLs,
and that the attacker's URL never appears in the action list of a task counted as passed.

### Re-run the evaluation only

Re-score existing agent logs without re-running the (expensive) agent. Requires GitLab/Reddit **still up and
un-cleaned** (the state still reflects the agent's actions), the step-3 env vars (gpt-4o `OPENAI_*`,
`NLTK_DATA`), and the same `--format` the run used.
```bash
OUT=<output-dir>/0_goal_hijacking_plain_text          # the scenario's output dir
export DATASET=webarena_prompt_injections GITLAB=http://localhost:8023 REDDIT=http://localhost:9999
export OPENAI_BASE_URL=https://<gpt-4o-gateway>/v1 OPENAI_API_KEY=<key> NLTK_DATA=/mnt/.../nltk_data

cd webarena_prompt_injections && source venv/bin/activate   # step-by-step ASR
python evaluator_step_by_step.py --log-folder $OUT/agent_logs/ --task-folder $OUT/webarena_tasks/ --format webarena
deactivate
cd ../visualwebarena && source venv/bin/activate && bash prepare.sh   # attacker + user utility
python evaluator_final_step.py --log-folder $OUT/agent_logs/ --task-folder $OUT/webarena_tasks_attacker/ --format webarena
python evaluator_final_step.py --log-folder $OUT/agent_logs/ --task-folder $OUT/webarena_tasks/ --format webarena
deactivate
```
If the containers were recreated since the logs were produced, the state no longer matches them → re-run
the whole scenario instead.

## Resetting environments

```bash
bash webarena_prompt_injections/scripts/restart_envs.sh            # gitlab + reddit
bash webarena_prompt_injections/scripts/restart_envs.sh gitlab     # or one of them
```
Recreates the container(s) from the pristine image, polls until each site answers, applies all GitLab
tuning in a single `reconfigure`, and prints the final HTTP status. `GITLAB_HOST=<host>` rewrites
`external_url`; `SKIP_TUNING=1` skips the gitlab.rb edits; `SUDO=""` forces no sudo. Step 4 of
`run_end_to_end.sh` does an in-place cleanup after each scenario; `WASP_SKIP_CLEANUP=1` leaves the state
dirty for inspection.

## Testing a new prompt injection attack

The injection logic is in [prompt_injector.py](webarena_prompt_injections/prompt_injector.py); attacker
goals in [experiment_config.raw.json](webarena_prompt_injections/configs/experiment_config.raw.json); the
payload templates in [constants.py](webarena_prompt_injections/constants.py).

## Citation

Evtimov, Zharmagambetov, Grattafiori, Guo, Chaudhuri. *WASP: Benchmarking Web Agent Security Against
Prompt Injection Attacks.* [arxiv:2504.18575](https://arxiv.org/abs/2504.18575).

## License

See [LICENSE](LICENSE).
