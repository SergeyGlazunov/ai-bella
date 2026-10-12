#!/bin/sh
set -eu
apk add --no-cache curl >/dev/null
pw=$(sed -n 's/^NEO4J_PASSWORD=//p' /env | tr -d '\r')
echo "pw_len=${#pw}"
code=$(curl -s -o /tmp/out -w '%{http_code}' -u "neo4j:${pw}" \
  -H 'Content-Type: application/json' \
  --data '{"statements":[{"statement":"RETURN 1 AS ok"}]}' \
  http://ai2-neo4j:7474/db/neo4j/tx/commit)
echo "http=$code"
head -c 200 /tmp/out
echo
