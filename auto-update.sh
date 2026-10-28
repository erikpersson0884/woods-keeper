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

declare -A PULLED
declare -A STACKS

for cid in $(docker ps -q); do
  img=$(docker inspect -f '{{.Config.Image}}' "$cid")
  is_latest "$img" || continue

  if [[ -z "${PULLED[$img]:-}" ]]; then
    docker pull -q "$img" >/dev/null 2>&1 || { log "pull failed: $img"; PULLED[$img]=fail; continue; }
    PULLED[$img]=ok
  fi
  [[ "${PULLED[$img]}" == ok ]] || continue

  running=$(docker inspect -f '{{.Image}}' "$cid")
  newest=$(docker image inspect -f '{{.Id}}' "$img")
  [[ "$running" == "$newest" ]] && continue

  project=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$cid")
  if [[ -z "$project" ]]; then
    log "$img has an update but container $cid isn't part of a stack, skipping"
    continue
  fi
  log "update found: $img (stack: $project)"
  STACKS[$project]=1
done

if [ ${#STACKS[@]} -eq 0 ]; then
  log "All stacks are up to date."
  exit 0
fi

for project in "${!STACKS[@]}"; do
  read -r id ep < <(curl -s -H "X-API-Key: $API_KEY" "$PORTAINER_URL/api/stacks" \
    | jq -r --arg n "$project" '.[] | select(.Name | ascii_downcase == $n) | "\(.Id) \(.EndpointId)"')
  if [[ -z "${id:-}" ]]; then
    log "stack $project not found in Portainer, skipping"
    continue
  fi
  log "redeploying $project (stack $id, endpoint $ep)"
  
  # Perform update-stack logic directly
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
