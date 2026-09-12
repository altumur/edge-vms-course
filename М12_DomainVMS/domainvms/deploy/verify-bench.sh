#!/usr/bin/env bash
# М12 on a real bench. Needs: federated regions (deploy/federation.hcl), the
# signer, agent, console and gateway jobs registered, and a management token.
#
#   deploy/verify-bench.sh north south
set -u
DOMAIN="${1:?domain cluster region}"; OTHER="${2:?another region}"

echo "== 1. two regions, one gossip pool"
nomad server members | grep -E "$DOMAIN|$OTHER" || { echo "regions not federated"; exit 1; }

echo "== 2. a Variable in $OTHER read from $DOMAIN through forwarding (the federated read)"
nomad var list -region "$OTHER" vms/ | head -5

echo "== 3. the agent in $OTHER may write domain/* and nothing else"
TOKEN="$(nomad acl token create -region "$OTHER" -type client -policy domain-agent -json | python3 -c 'import sys,json; print(json.load(sys.stdin)["SecretID"])')"
NOMAD_TOKEN="$TOKEN" nomad var put -region "$OTHER" -force domain/keys current=probe >/dev/null && echo "domain/keys: write ok"
if NOMAD_TOKEN="$TOKEN" nomad var put -region "$OTHER" -force vms/cameras/999 name=x 2>/dev/null; then
  echo "FAIL: the agent could write vms/cameras/999"; exit 1
else echo "vms/cameras/999: 403 (correct)"; fi

echo "== 4. the domain cluster is a stated decision, visible in the directory"
nomad var get -region "$DOMAIN" domain/signer >/dev/null && echo "signer keys in $DOMAIN's raft"
nomad var get -region "$OTHER" domain/signer >/dev/null 2>&1 && { echo "FAIL: signer keys in $OTHER"; exit 1; } || echo "not in $OTHER (correct)"

echo "== 5. the cold start order: signer before any certificate"
nomad job status domain-signer | grep -q running && echo "signer running" || { echo "signer not running"; exit 1; }

echo "== 6. the console lists both clusters, and says when one is unreachable"
curl -s "http://console.$DOMAIN:8443/api/cameras?size=1" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("clusters:", d["clusters"], "complete:", d["complete"])'
echo "now: nomad node drain the whole of $OTHER (or pull its uplink) and re-run the curl — expect $OTHER: unreachable, complete: false, rows still listed"
