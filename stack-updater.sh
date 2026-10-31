#!/usr/bin/env bash
set -euo pipefail

# Keep shell-generated log timestamps aligned with the dashboard.
export TZ="${TZ:-Europe/Stockholm}"

log() { echo "$(date '+%F %T') $*"; }

TARGET_STACK_ID="${1:-}"

# Different stacks may update concurrently, but a given stack is still
# serialized when this script is invoked independently.
LOCK_NAME="${TARGET_STACK_ID:-global}"
LOCK_NAME="${LOCK_NAME//[^a-zA-Z0-9_.-]/_}"
exec 9>"/tmp/auto-update-${LOCK_NAME}.lock"
flock -n 9 || exit 0

if [[ "${PORTAINER_URL:-}" != https://* ]] \
  && { [[ "${PORTAINER_URL:-}" != http://* ]] \
    || [[ "${ALLOW_INSECURE_PORTAINER_HTTP:-}" != "true" ]]; }; then
  log "PORTAINER_URL must use HTTPS (set ALLOW_INSECURE_PORTAINER_HTTP=true only for a trusted private network)"
  exit 1
fi

is_latest() {
  [[ "$1" == *@* ]] && return 1
  local name="${1##*/}"
  [[ "$name" != *:* || "$name" == *:latest ]]
}

redeploy_stack() {
  local id="$1"
  local stack
  local ep
  local file
  local env

  stack=$(curl -fsS -H "X-API-Key: $API_KEY" "$PORTAINER_URL/api/stacks/$id")
  ep=$(echo "$stack" | jq -r '.EndpointId')
  name=$(echo "$stack" | jq -r '.Name // "unknown"')
  if [[ -z "$ep" || "$ep" == "null" ]]; then
    log "stack $id has no endpoint, skipping"
    return 1
  fi

  log "redeploying stack $name (id $id, endpoint $ep)"

  file=$(curl -fsS -H "X-API-Key: $API_KEY" "$PORTAINER_URL/api/stacks/$id/file" \
    | jq -r '.StackFileContent')
  env=$(echo "$stack" | jq '.Env // []')

  result=$(jq -n --arg f "$file" --argjson env "$env" \
    '{stackFileContent: $f, env: $env, prune: false, pullImage: true}' \
    | curl -fsS -X PUT "$PORTAINER_URL/api/stacks/$id?endpointId=$ep" \
      -H "X-API-Key: $API_KEY" \
      -H "Content-Type: application/json" \
      -d @-)
  status=$(echo "$result" | jq -r '.Status // "unknown"')
  log "stack $name ($id) redeployed on endpoint $ep (status: $status)"
}

is_excluded_stack() {
  local id="$1"
  [[ ",${EXCLUDED_STACK_IDS:-}," == *",$id,"* ]]
}

if [[ -n "$TARGET_STACK_ID" ]]; then
  redeploy_stack "$TARGET_STACK_ID"
  exit $?
fi

PULLED_IMAGES=""
STACK_PROJECTS=""

for cid in $(docker ps -q); do
  img=$(docker inspect -f '{{.Config.Image}}' "$cid")
  is_latest "$img" || continue

  # Check if we already pulled this image during this run
  if [[ "$PULLED_IMAGES" != *"$img"* ]]; then
    if docker pull -q "$img" >/dev/null 2>&1; then
      PULLED_IMAGES="$PULLED_IMAGES $img"
    else
      log "pull failed: $img"
      continue
    fi
  fi

  running=$(docker inspect -f '{{.Image}}' "$cid")
  newest=$(docker image inspect -f '{{.Id}}' "$img")
  [[ "$running" == "$newest" ]] && continue

  project=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$cid" 2>/dev/null || echo "")
  if [[ -z "$project" || "$project" == "<no value>" ]]; then
    log "$img has an update but container $cid isn't part of a stack, skipping"
    continue
  fi

  # Add to our list of stacks if not already listed
  if [[ "$STACK_PROJECTS" != *"$project"* ]]; then
    log "update found: $img (stack: $project)"
    STACK_PROJECTS="$STACK_PROJECTS $project"
  fi
done

if [[ -z "${STACK_PROJECTS// }" ]]; then
  log "No stack-managed container updates found."
  exit 0
fi

# Redeploy each affected stack
for project in $STACK_PROJECTS; do
  read -r id ep < <(curl -s -H "X-API-Key: $API_KEY" "$PORTAINER_URL/api/stacks" \
    | jq -r --arg n "$project" '.[] | select(.Name | ascii_downcase == $n) | "\(.Id) \(.EndpointId)"')
  
  if [[ -z "${id:-}" || "$id" == "null" ]]; then
    log "stack $project not found in Portainer, skipping"
    continue
  fi

  if is_excluded_stack "$id"; then
    log "skipping $project (stack $id is excluded from global updates)"
    continue
  fi
  
  redeploy_stack "$id"
done