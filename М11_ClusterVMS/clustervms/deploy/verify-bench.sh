#!/usr/bin/env bash
# М11 — the checks that need a real cluster, in one run. PASS/FAIL per item.
#
#   deploy/verify-bench.sh          # with NOMAD_ADDR / NOMAD_TOKEN (management) set
#
# 1. Nomad >= 1.8.0 (the disconnect block)
# 2. three servers, one leader; the Podman driver healthy on every client; meta.archive set somewhere
# 3. the six jobspecs validate (worker, controller, recorder, reccontroller, console, resource) and the autoscaler's
# 4. the ACL: a token carrying ONLY vmsworker-policy writes vms/epoch/* and vms/slots/*, and is
#    refused on vms/cameras/* — one writer per key, enforced rather than promised
# 5. the same from INSIDE a vmsworker allocation with the task's own workload-identity token
# 6. scale out and in: `nomad job scale vmsworker N+1` → a new slot claimed; `… N` → the slot released
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
pass=0; fail=0
ok()   { echo "  PASS  $*"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $*"; fail=$((fail+1)); }

# 1
ver="$(nomad version | head -1 | sed -E 's/.*v([0-9]+\.[0-9]+\.[0-9]+).*/\1/')"
if [ "$(printf '%s\n1.8.0\n' "$ver" | sort -V | head -1)" = "1.8.0" ]; then ok "nomad $ver >= 1.8.0"; else bad "nomad $ver < 1.8.0 — the disconnect block does not exist"; fi

# 2
leaders="$(nomad server members 2>/dev/null | awk 'NR>1 && $5=="true"' | wc -l)"
servers="$(nomad server members 2>/dev/null | awk 'NR>1' | wc -l)"
[ "$servers" -ge 3 ] && [ "$leaders" -eq 1 ] && ok "$servers servers, 1 leader" || bad "$servers servers, $leaders leaders"
archives=0
for id in $(nomad node status -quiet 2>/dev/null); do
  if nomad node status -verbose "$id" 2>/dev/null | grep -qE '^podman +true'; then ok "podman driver healthy on $id"; else bad "podman driver NOT healthy on $id"; fi
  nomad node status -verbose "$id" 2>/dev/null | grep -qE '^archive ' && archives=$((archives+1))
done
[ "$archives" -ge 1 ] && ok "$archives server(s) declare meta.archive (the resource has somewhere to be)" || bad "no server declares meta.archive"

# 3
for j in vmsworker vmscontroller vmsrecorder vmsreccontroller console resource autoscaler; do
  nomad job validate "$HERE/$j.nomad.hcl" >/dev/null 2>&1 && ok "$j.nomad.hcl validates" || bad "$j.nomad.hcl: $(nomad job validate "$HERE/$j.nomad.hcl" 2>&1 | tail -1)"
done

# 4 — policy semantics with a token that carries ONLY the worker's policy
nomad acl policy apply -description vmsworker vmsworker "$HERE/vmsworker-policy.hcl" >/dev/null 2>&1
nomad acl policy apply -description vmscontroller vmscontroller "$HERE/vmscontroller-policy.hcl" >/dev/null 2>&1
nomad acl policy apply -description vmsrecorder vmsrecorder "$HERE/vmsrecorder-policy.hcl" >/dev/null 2>&1
nomad acl policy apply -description vmsreccontroller vmsreccontroller "$HERE/vmsreccontroller-policy.hcl" >/dev/null 2>&1
nomad acl policy apply -description resource resource "$HERE/resource-policy.hcl" >/dev/null 2>&1
nomad acl policy apply -description console console "$HERE/console-policy.hcl" >/dev/null 2>&1
tok="$(nomad acl token create -type client -policy vmsworker -ttl 10m -json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["SecretID"])')"
if [ -n "$tok" ]; then
  NOMAD_TOKEN="$tok" nomad var put -force vms/epoch/verify epoch=1 >/dev/null 2>&1 && ok "worker token writes vms/epoch/*" || bad "worker token cannot write its epochs"
  NOMAD_TOKEN="$tok" nomad var put -force vms/slots/w-verify holder=probe >/dev/null 2>&1 && ok "worker token writes vms/slots/*" || bad "worker token cannot claim a slot"
  NOMAD_TOKEN="$tok" nomad var put -force objects/vms/w-verify/heartbeat data='{}' >/dev/null 2>&1 && ok "worker token writes its heartbeat object" || bad "worker token cannot write objects/vms/*"
  nomad var purge objects/vms/w-verify/heartbeat >/dev/null 2>&1
  if NOMAD_TOKEN="$tok" nomad var put -force vms/cameras/verify name=tampered >/dev/null 2>&1; then bad "worker token wrote vms/cameras/* — one writer per key is NOT enforced"; else ok "worker token refused on vms/cameras/* (403)"; fi
  nomad var purge vms/epoch/verify >/dev/null 2>&1; nomad var purge vms/slots/w-verify >/dev/null 2>&1
else
  bad "could not create a client token with policy vmsworker (ACLs bootstrapped? NOMAD_TOKEN set?)"
fi
# 4b — the console's token: the operator's rows, never placement
ctok="$(nomad acl token create -type client -policy console -ttl 10m -json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["SecretID"])')"
if [ -n "$ctok" ]; then
  NOMAD_TOKEN="$ctok" nomad var put -force vms/cameras/verify name=probe >/dev/null 2>&1 && ok "console token writes vms/cameras/*" || bad "console token cannot write a camera row"
  NOMAD_TOKEN="$ctok" nomad var put -force vms/idem/verify state=done >/dev/null 2>&1 && ok "console token writes vms/idem/* (a retry answered by any instance)" || bad "console token cannot write vms/idem/*"
  if NOMAD_TOKEN="$ctok" nomad var put -force vms/placement/verify worker=w-0 >/dev/null 2>&1; then bad "console token wrote vms/placement/* — a console that can place is a second controller"; else ok "console token refused on vms/placement/* (403)"; fi
  if NOMAD_TOKEN="$ctok" nomad var put -force vms/workers/w-verify units=1 >/dev/null 2>&1; then bad "console token wrote vms/workers/*"; else ok "console token refused on vms/workers/* (403)"; fi
  nomad var purge vms/cameras/verify >/dev/null 2>&1
fi

# 5 — the binding to the JOB's workload identity, which is what the product relies on
nomad acl policy apply -namespace default -job vmsworker vmsworker "$HERE/vmsworker-policy.hcl" >/dev/null 2>&1 && ok "policy bound to job vmsworker" || bad "policy binding to job failed"
nomad acl policy apply -namespace default -job vmscontroller vmscontroller "$HERE/vmscontroller-policy.hcl" >/dev/null 2>&1 && ok "policy bound to job vmscontroller" || bad "policy binding to vmscontroller failed"
nomad acl policy apply -namespace default -job vmsrecorder vmsrecorder "$HERE/vmsrecorder-policy.hcl" >/dev/null 2>&1 && ok "policy bound to job vmsrecorder" || bad "policy binding to vmsrecorder failed"
nomad acl policy apply -namespace default -job vmsreccontroller vmsreccontroller "$HERE/vmsreccontroller-policy.hcl" >/dev/null 2>&1 && ok "policy bound to job vmsreccontroller" || bad "policy binding to vmsreccontroller failed"
nomad acl policy apply -namespace default -job console console "$HERE/console-policy.hcl" >/dev/null 2>&1 && ok "policy bound to job console" || bad "policy binding to console failed"
alloc="$(nomad job allocs -json vmsworker 2>/dev/null | python3 -c 'import sys,json;a=[x for x in json.load(sys.stdin) if x["ClientStatus"]=="running"];print(a[0]["ID"] if a else "")')"
if [ -n "$alloc" ]; then
  inside='H="X-Nomad-Token: $NOMAD_TOKEN"; A="${NOMAD_ADDR:-http://127.0.0.1:4646}";
    own=$(curl -s -o /dev/null -w "%{http_code}" -X PUT -H "$H" -d "{\"Items\":{\"epoch\":\"1\"}}" "$A/v1/var/vms/epoch/verify");
    oth=$(curl -s -o /dev/null -w "%{http_code}" -X PUT -H "$H" -d "{\"Items\":{\"name\":\"x\"}}" "$A/v1/var/vms/cameras/verify");
    echo "own=$own other=$oth"'
  res="$(nomad alloc exec -task vmsworker "$alloc" sh -c "$inside" 2>/dev/null)"
  case "$res" in
    own=200*other=403*) ok "workload identity: $res — one writer per key holds";;
    *) bad "workload identity: $res (want own=200 other=403)";;
  esac
  nomad var purge vms/epoch/verify >/dev/null 2>&1
else
  bad "no running allocation of vmsworker to test the workload-identity token inside (run the job first)"
fi

# 5a — the mirror is resource to resource: a peer accepts a PUT and lists it; nothing went through a store
res="$(nomad service info -json resource 2>/dev/null | python3 -c 'import sys,json;a=json.load(sys.stdin);print(f"{a[0][\"Address\"]}:{a[0][\"Port\"]}" if a else "")' 2>/dev/null)"
if [ -n "$res" ]; then
  curl -s -o /dev/null -w "%{http_code}" -X PUT --data-binary '{"t":0,"kind":"probe"}' "http://$res/mirror/srv-verify/vms/0/e1/19700101T000000Z.events.jsonl" | grep -q 204 \
    && curl -s "http://$res/mirrored/srv-verify" | grep -q '"path"' && ok "mirror: a peer took a copy and lists it" || bad "mirror: PUT/GET on the resource failed"
else
  bad "no resource service registered to test the mirror against"
fi

# 6 — scale out, then in: the slot claimed, then released; the controller asked for neither
n="$(nomad job status -json vmsworker 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["TaskGroups"][0]["Count"])' 2>/dev/null)"
if [ -n "$n" ]; then
  nomad job scale vmsworker $((n+1)) >/dev/null 2>&1; sleep 20
  if nomad var get "vms/slots/w-$n" 2>/dev/null | grep -q 'released *= *false'; then ok "scale out: slot w-$n claimed by the new allocation"; else bad "scale out: slot w-$n not claimed"; fi
  nomad job scale vmsworker "$n" >/dev/null 2>&1; sleep 30
  if nomad var get "vms/slots/w-$n" 2>/dev/null | grep -q 'released *= *true'; then ok "scale in: slot w-$n released (SIGTERM inside kill_timeout)"; else bad "scale in: slot w-$n not released — kill_timeout too short, or the stop was not orderly"; fi
else
  bad "vmsworker is not running; skipped the scale drill"
fi

echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
