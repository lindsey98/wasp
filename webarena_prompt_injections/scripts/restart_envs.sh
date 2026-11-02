#!/usr/bin/env bash
# Restart the WASP web environments from their pristine images so a run starts from a clean state.
#
# Why this exists: GitLab/Reddit keep whatever earlier runs did (comments, deploy tokens, deleted
# projects, a public profile). The attacker grader reads live state, so leftovers inflate attacker
# utility; a leftover comment identical to the user's task text also gets tainted by the guard and
# wedges the agent. Recreating the containers is the only reliable reset.
#
# Usage:
#   scripts/restart_envs.sh            # gitlab + reddit
#   scripts/restart_envs.sh gitlab     # just one
#   scripts/restart_envs.sh reddit
#
# Env (all optional):
#   GITLAB_HOST    hostname to bake into GitLab's external_url (default: leave the image's value)
#   GITLAB_IMAGE   default gitlab-populated-final-port8023
#   REDDIT_IMAGE   default postmill-populated-exposed-withimg
#   GITLAB_PORT    default 8023          REDDIT_PORT  default 9999
#   SUDO           "sudo" / "" ; auto-detected (sudo only if plain `docker ps` fails)
#   SKIP_TUNING=1  skip the gitlab.rb tuning (gravatar/prometheus off, 500 pg connections, 2 puma workers)
set -euo pipefail

GITLAB_IMAGE=${GITLAB_IMAGE:-gitlab-populated-final-port8023}
REDDIT_IMAGE=${REDDIT_IMAGE:-postmill-populated-exposed-withimg}
GITLAB_PORT=${GITLAB_PORT:-8023}
REDDIT_PORT=${REDDIT_PORT:-9999}
GITLAB_CONTAINER=gitlab
REDDIT_CONTAINER=forum

if [ -z "${SUDO+x}" ]; then
    if docker ps >/dev/null 2>&1; then SUDO=""; else SUDO="sudo"; fi
fi
D="$SUDO docker"

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# wait_http URL TIMEOUT_SEC -- poll until the URL answers with any HTTP status (2xx/3xx/4xx all mean "up").
wait_http() {
    local url=$1 timeout=${2:-600} t=0 code
    while [ "$t" -lt "$timeout" ]; do
        code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" || true)
        if [ "$code" != "000" ] && [ "$code" != "502" ] && [ "$code" != "503" ]; then
            echo "  $url -> HTTP $code (after ${t}s)"; return 0
        fi
        sleep 5; t=$((t + 5))
    done
    echo "  $url did not come up within ${timeout}s" >&2; return 1
}

restart_gitlab() {
    log "GitLab: recreating container '$GITLAB_CONTAINER' from $GITLAB_IMAGE"
    $D rm -f "$GITLAB_CONTAINER" >/dev/null 2>&1 || true
    $D run --name "$GITLAB_CONTAINER" -d --shm-size=2g -p "$GITLAB_PORT:$GITLAB_PORT" "$GITLAB_IMAGE" \
        /opt/gitlab/embedded/bin/runsvdir-start >/dev/null
    log "GitLab: waiting for first boot"
    wait_http "http://localhost:$GITLAB_PORT" 900

    if [ -n "${GITLAB_HOST:-}" ]; then
        log "GitLab: external_url -> http://$GITLAB_HOST:$GITLAB_PORT"
        $D exec "$GITLAB_CONTAINER" sed -i \
            "s|^external_url.*|external_url 'http://$GITLAB_HOST:$GITLAB_PORT'|" /etc/gitlab/gitlab.rb
    fi
    if [ "${SKIP_TUNING:-0}" != "1" ]; then
        # All gitlab.rb edits in one go -> ONE reconfigure (each reconfigure is minutes).
        log "GitLab: gitlab.rb tuning (gravatar/prometheus off, pg max_connections 500, puma workers 2)"
        $D exec "$GITLAB_CONTAINER" bash -lc 'cat >> /etc/gitlab/gitlab.rb <<EOF

# --- WASP restart_envs.sh ---
gitlab_rails["gravatar_enabled"] = false
prometheus_monitoring["enable"] = false
postgresql["max_connections"] = 500
puma["worker_processes"] = 2
EOF'
    fi
    if [ -n "${GITLAB_HOST:-}" ] || [ "${SKIP_TUNING:-0}" != "1" ]; then
        log "GitLab: gitlab-ctl reconfigure"
        $D exec "$GITLAB_CONTAINER" gitlab-ctl reconfigure >/dev/null
    fi

    log "GitLab: application settings (open signup, no admin approval, prometheus metrics off)"
    $D exec "$GITLAB_CONTAINER" gitlab-rails runner '
s = ApplicationSetting.current
s.signup_enabled = true
s.require_admin_approval_after_user_signup = false
s.prometheus_metrics_enabled = false
s.save!
puts "signup=#{s.signup_enabled} approval=#{s.require_admin_approval_after_user_signup} prom_metrics=#{s.prometheus_metrics_enabled}"'

    log "GitLab: gitlab-ctl restart"
    $D exec "$GITLAB_CONTAINER" gitlab-ctl restart >/dev/null
    wait_http "http://localhost:$GITLAB_PORT" 600
}

restart_reddit() {
    log "Reddit: recreating container '$REDDIT_CONTAINER' from $REDDIT_IMAGE"
    $D rm -f "$REDDIT_CONTAINER" >/dev/null 2>&1 || true
    $D run --name "$REDDIT_CONTAINER" -d -p "$REDDIT_PORT:80" \
        -e RATELIMIT_WHITELIST=0.0.0.0/0,::/0 "$REDDIT_IMAGE" >/dev/null
    log "Reddit: waiting for boot"
    wait_http "http://localhost:$REDDIT_PORT" 300
}

targets=("$@")
[ ${#targets[@]} -eq 0 ] && targets=(gitlab reddit)
for t in "${targets[@]}"; do
    case "$t" in
        gitlab) restart_gitlab ;;
        reddit) restart_reddit ;;
        *) echo "unknown target '$t' (gitlab|reddit)" >&2; exit 2 ;;
    esac
done

log "Done."
for t in "${targets[@]}"; do
    case "$t" in
        gitlab) printf '  gitlab  http://localhost:%s  -> %s\n' "$GITLAB_PORT" "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$GITLAB_PORT")" ;;
        reddit) printf '  reddit  http://localhost:%s  -> %s\n' "$REDDIT_PORT" "$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$REDDIT_PORT")" ;;
    esac
done
