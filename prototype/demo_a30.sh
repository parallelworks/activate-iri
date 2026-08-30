#!/usr/bin/env bash
# Terminal demo: drive the public IRI endpoint with your ACTIVATE credential and run a GPU
# inventory job on the lab A30 server. Every call is printed with its status and timing so the
# API path is visible: client -> IRI endpoint -> ACTIVATE workflow run -> cluster scheduler.
#
#   PW_API_KEY=pwt_... ./demo_a30.sh                # or rely on the pw CLI context
#   BASE=http://127.0.0.1:8100/api/v2 ./demo_a30.sh # against the local endpoint
set -uo pipefail
BASE=${BASE:-https://activate-iri.activate.pw/api/v2}
RESOURCE=${RESOURCE:-Lab A30 GPU server}
PRESET=${PRESET:-gpu}    # gpu | container | hello
TOKEN=${PW_API_KEY:-$(python3 -c 'import json,os; d=json.load(open(os.path.expanduser("~/.config/pw/credentials")))["identities"]["activate"]; print(d.get("apikey") or d.get("token"))')}
auth=(-H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json")
call() {  # method path [body]
  local t0=$(date +%s%N); local out
  if [ -n "${3:-}" ]; then out=$(curl -sS -w '\n%{http_code}' "${auth[@]}" -X "$1" "$BASE$2" -d "$3"); else out=$(curl -sS -w '\n%{http_code}' "${auth[@]}" -X "$1" "$BASE$2"); fi
  local code=$(echo "$out" | tail -1); local ms=$(( ($(date +%s%N) - t0) / 1000000 ))
  printf '  %-6s %-58s %s  %4d ms\n' "$1" "$2" "$code" "$ms" >&2
  echo "$out" | sed '$d'
}
wait_task() { local path; path=$(echo "$1" | sed -E "s#^https?://[^/]+/api/v[0-9]+##"); for i in $(seq 1 90); do t=$(call GET "$path"); s=$(echo "$t" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])'); case "$s" in completed|failed|canceled) echo "$t"; return;; esac; sleep 1.5; done; echo '{"status":"timeout"}'; }

echo "== 1. who am I (facility-specific credential)"; ME=$(call GET /account/whoami | python3 -c 'import sys,json; print(json.load(sys.stdin)["username"])'); echo "   $ME"
echo "== 2. discover the compute resource"; RID=$(call GET /compute/resources | python3 -c "import sys,json; r=[x for x in json.load(sys.stdin) if x['name']=='$RESOURCE']; print(r[0]['id'] if r else '')"); [ -n "$RID" ] || { echo "resource '$RESOURCE' not found"; exit 2; }; echo "   $RESOURCE -> $RID"
WORK=/home/$(whoami)/iri-demo-$(date -u +%H%M%S)
echo "== 3. working directory via the filesystem task loop"; T=$(call POST "/filesystem/mkdir/$RID" "{\"path\": \"$WORK\", \"parent\": true}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_uri"])'); wait_task "$T" >/dev/null
case "$PRESET" in
  gpu) SPEC=$(python3 -c "import json; w='$WORK'; print(json.dumps({'executable':'/bin/bash','arguments':['-c','hostname; nvidia-smi --query-gpu=name,memory.total,utilization.gpu --format=csv'],'name':'iri-gpu-inventory','directory':w,'stdout_path':w+'/out.log','stderr_path':w+'/err.log','resources':{'node_count':1,'process_count':1,'exclusive_node_use':False},'attributes':{'duration':600,'queue_name':'debug'}}))");;
  container) SPEC=$(python3 -c "import json; w='$WORK'; print(json.dumps({'executable':'nvidia-smi','container':{'image':'docker://nvidia/cuda:12.4.1-base-ubuntu22.04'},'name':'iri-cuda-container','directory':w,'stdout_path':w+'/out.log','stderr_path':w+'/err.log','resources':{'node_count':1,'exclusive_node_use':False},'attributes':{'duration':900,'queue_name':'debug','custom_attributes':{'apptainer-nv':'true'}}}))");;
  *) SPEC=$(python3 -c "import json; w='$WORK'; print(json.dumps({'executable':'/bin/bash','arguments':['-c','hostname; date -u; echo hello from the IRI API'],'name':'iri-hello','directory':w,'stdout_path':w+'/out.log','resources':{'node_count':1,'exclusive_node_use':False},'attributes':{'duration':300,'queue_name':'debug'}}))");;
esac
echo "== 4. submit the PSI/J job (routed through an ACTIVATE workflow run to the cluster)"; JOB=$(call POST "/compute/job/$RID" "$SPEC"); JID=$(echo "$JOB" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("id",""))'); [ -n "$JID" ] || { echo "$JOB"; exit 3; }; echo "   job $JID: $(echo "$JOB" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["status"]["state"], d["status"].get("meta_data",{}).get("cluster"))')"
echo "== 5. follow it"; for i in $(seq 1 80); do ST=$(call GET "/compute/status/$RID/$JID?historical=true" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status",{}).get("state","?") if isinstance(d.get("status"),dict) else d)'); echo "   $(date -u +%T) $ST"; case "$ST" in completed|failed|canceled) break;; esac; sleep 5; done
echo "== 6. stdout back through the filesystem API"; T=$(call POST "/filesystem/download/$RID" "{\"path\": \"$WORK/out.log\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_uri"])'); wait_task "$T" | python3 -c 'import sys,json,base64; d=json.load(sys.stdin); print(base64.b64decode(d["result"]["output"]).decode() if d["status"]=="completed" else d)'
echo "== 7. clean up"; T=$(call POST "/filesystem/rm/$RID" "{\"path\": \"$WORK\"}" | python3 -c 'import sys,json; print(json.load(sys.stdin)["task_uri"])'); wait_task "$T" >/dev/null; echo "   done"
