#!/usr/bin/env bash
set -euo pipefail

# Don't run twice at once
exec 9>/tmp/auto-update.lock
flock -n 9 || exit 0

log() { echo "$(date '+%F %T') $*"; }

is_latest() {
  [[ "$1" == *@* ]] && return 1
  local name="${1##*/}"
  [[ "$name" != *:* || "$name" == *:latest ]]
}

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
  
  log "redeploying $project (stack $id, endpoint $ep)"
  
  STACK=$(curl -s -H "X-API-Key: $API_KEY" "$PORTAINER_URL/api/stacks/$id")
  FILE=$(curl -s -H "X-API-Key: $API_KEY" "$PORTAINER_URL/api/stacks/$id/file" | jq -r '.StackFileContent')
  ENV=$(echo "$STACK" | jq '.Env // []')

  jq -n --arg f "$FILE" --argjson env "$ENV" \
    '{stackFileContent: $f, env: $env, prune: false, pullImage: true}' \
  | curl -s -X PUT "$PORTAINER_URL/api/stacks/$id?endpointId=$ep" \
    -H "X-API-Key: $API_KEY" \
    -H "Content-Type: application/json" \
    -d @- | jq '{Id, Name, Status}'
done