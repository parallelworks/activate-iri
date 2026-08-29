#!/usr/bin/env bash
# Live smoke test of the prototype endpoint (IRI v2 verbs): identity, discovery, allocations,
# a real Slurm job on the lab cluster, and the asynchronous filesystem loop. Bearer credential
# is the caller's ACTIVATE platform token from the pw CLI context.
set -uo pipefail
BASE=${BASE:-http://127.0.0.1:8100/api/v2}
TOKEN=$(python3 -c 'import json,os; d=json.load(open(os.path.expanduser("~/.config/pw/credentials")))["identities"]["activate"]; print(d.get("apikey") or d.get("token"))')
auth=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")
api() { curl -sS "${auth[@]}" "$@"; }
step() { printf '\n== %s\n' "$*"; }
wait_task() {  # $1 task_uri
  for i in $(seq 1 60); do
    t=$(api "$1"); s=$(echo "$t" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
    case "$s" in completed) echo "$t"; return 0;; failed|canceled) echo "$t"; return 1;; esac
    sleep 1
  done; echo "timeout"; return 1
}
pass=0; fail=0
check() { if [ "$1" -eq 0 ]; then pass=$((pass+1)); echo "   ok"; else fail=$((fail+1)); echo "   FAIL"; fi; }

step "whoami"; api "$BASE/account/whoami"; echo; check $?
step "compute resources"; RID=$(api "$BASE/compute/resources" | python3 -c 'import sys,json; r=[x for x in json.load(sys.stdin) if x["name"]=="labcluster"]; print(r[0]["id"])'); echo "labcluster resource: $RID"; check $([ -n "$RID" ]; echo $?)
step "projects and allocations"; api "$BASE/account/projects" | python3 -c 'import sys,json; ps=json.load(sys.stdin); print(len(ps), "projects:", [p["name"] for p in ps][:6])'; check $?
step "storage locations"; api "$BASE/storage/locations/$RID" | python3 -c 'import sys,json; [print("  ", l["logical_name"], l["path"]) for l in json.load(sys.stdin)]'; check $?

WORK=$HOME/iri-smoke-$(date -u +%Y%m%dT%H%M%SZ)
step "filesystem mkdir $WORK"; T=$(api -X POST "$BASE/filesystem/mkdir/$RID" -d "{\"path\": \"$WORK\", \"parent\": true}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_uri"])'); wait_task "$T" >/dev/null; check $?
step "filesystem upload"; printf 'hello from the IRI smoke test\n' > /tmp/iri-smoke.txt; T=$(curl -sS -H "Authorization: Bearer ${TOKEN}" -X POST "$BASE/filesystem/upload/$RID?path=$WORK/hello.txt" -F "file=@/tmp/iri-smoke.txt" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_uri"])'); wait_task "$T" >/dev/null; check $?
step "filesystem ls"; T=$(api -X POST "$BASE/filesystem/ls/$RID" -d "{\"path\": \"$WORK\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_uri"])'); wait_task "$T" | python3 -c 'import sys,json; [print("  ", f["permissions"], f["size"], f["name"]) for f in json.load(sys.stdin)["result"]["output"]]'; check $?
step "filesystem checksum"; T=$(api -X POST "$BASE/filesystem/checksum/$RID" -d "{\"path\": \"$WORK/hello.txt\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_uri"])'); wait_task "$T" | python3 -c 'import sys,json; print("  ", json.load(sys.stdin)["result"]["output"]["checksum"])'; check $?

step "submit Slurm job on labcluster (debug partition)"
SPEC=$(python3 - "$WORK" <<'PY'
import json,sys
w=sys.argv[1]
print(json.dumps({"executable":"/bin/bash","arguments":["-c","hostname; date -u; sleep 5; echo smoke-ok > result.txt"],"name":"iri-smoke","directory":w,
  "stdout_path":f"{w}/iri-smoke.out","stderr_path":f"{w}/iri-smoke.err","resources":{"node_count":1,"process_count":1,"exclusive_node_use":False},
  "attributes":{"duration":300,"queue_name":"debug"}}))
PY
)
JOB=$(api -X POST -H "Idempotency-Key: smoke-$(date +%s)" "$BASE/compute/job/$RID" -d "$SPEC"); echo "$JOB" | head -c 400; echo
JID=$(echo "$JOB" | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])'); check $([ -n "$JID" ]; echo $?)
step "poll job $JID"; state=""; for i in $(seq 1 60); do S=$(api "$BASE/compute/status/$RID/$JID?historical=true"); state=$(echo "$S" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"]["state"])'); echo "   $(date -u +%T) $state"; case "$state" in completed|failed|canceled) break;; esac; sleep 3; done; check $([ "$state" = completed ]; echo $?)
step "job stdout via filesystem download"; T=$(api -X POST "$BASE/filesystem/download/$RID" -d "{\"path\": \"$WORK/iri-smoke.out\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_uri"])'); wait_task "$T" | python3 -c 'import sys,json,base64; print(base64.b64decode(json.load(sys.stdin)["result"]["output"]).decode())'; check $?
step "list my jobs (historical)"; api -X POST "$BASE/compute/status/$RID?historical=true&limit=5" | python3 -c 'import sys,json; [print("  ", j["id"], j["status"]["state"]) for j in json.load(sys.stdin)]'; check $?
step "cleanup rm $WORK"; T=$(api -X POST "$BASE/filesystem/rm/$RID" -d "{\"path\": \"$WORK\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_uri"])'); wait_task "$T" >/dev/null; check $?
printf '\n%d passed, %d failed\n' "$pass" "$fail"; [ "$fail" -eq 0 ]
