#!/bin/sh
# rnti-mapper: every PERIOD seconds, rebuild /www/metrics with one
#   ran_ue_info{ran_node,ran_component,rnti,ran_ue_id,imsi,ue_name} 1
# line per UE context of the RRC of each gNB / CU / CU-CP, and serve it with busybox httpd.
#
# /rnti-mapper/sources: "<component> <namespace> <label selector> <oai|ocudu>" lines, one per RAN node running RRC.
#   oai:   nrRRC_stats.log, rewritten every second by the gNB/CU, copied from the metrics-parser sidecar (shared
#          log volume) or else from the RAN container working directory.
#   ocudu: the gNB/CU logs on stdout (info level): its "c-rnti=" / "ran_ue=" lines are appended incrementally
#          (kubectl logs --since-time); the file starts again with each new pod.
# Core: AMF log (OAI: last "UEs' Information" tables; Open5GS: attach lines, incremental) and, when set,
# the ue-mapper-api inventory (INVENTORY_URL): (gNB N3 IP, RAN UE NGAP id) <-> IMSI from the N2/PFCP captures.
# The IPs of each RRC node pod (pod network and Multus networks) let join.awk use the inventory per RAN node.
# Errors are printed to the container log when they change.
set -u
D=/tmp/rnti-mapper
mkdir -p /www "$D/ran"
: > /www/metrics
httpd -p "${PORT}" -h /www

OAI_FILES="/oai-gnb-logs/nrRRC_stats.log /opt/oai-gnb/nrRRC_stats.log"
OCUDU_PATTERN='c-rnti=0x|ran_ue='
OPEN5GS_PATTERN='InitialUEMessage|RAN_UE_NGAP_ID\[|Known UE by|Unknown UE by|\[suci-|Registration complete'

low() { if nice -n 19 true 2> /dev/null; then nice -n 19 "$@"; else "$@"; fi; }

# $1 namespace $2 label selector: first running pod ("pod/<name>")
first_pod() { kubectl -n "$1" get pod -l "$2" --field-selector=status.phase=Running -o name 2>> "$D/err" | head -1; }

# $1 namespace $2 pod $3 container (empty: default) $4 output $5 pattern: append the matching log lines written
# after the last saved timestamp of the same pod (kubectl --timestamps prefixes each line with an RFC 3339 time)
incremental_log() {
  state="$4.state" last=""
  if [ "$(cut -d' ' -f1 "$state" 2> /dev/null)" = "$2" ]; then last=$(cut -d' ' -f2 "$state"); else : > "$4"; fi
  kubectl -n "$1" logs "$2" ${3:+-c "$3"} --timestamps ${last:+--since-time="$last"} 2>> "$D/err" \
    | low grep -E "$5" | awk -v last="$last" '$1 > last' >> "$4"
  new=$(tail -n 1 "$4" | cut -d' ' -f1)
  echo "$2 ${new:-$last}" > "$state"
}

# $1 component $2 namespace $3 pod: "<component> <ip> ..." (pod IPs and the IPs of the Multus network-status)
node_ips() {
  ips=$(kubectl -n "$2" get "$3" -o jsonpath='{.status.podIPs[*].ip} {.metadata.annotations.k8s\.v1\.cni\.cncf\.io/network-status}' \
    2>> "$D/err" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | sort -u | tr '\n' ' ')
  echo "$1 $ips"
}

# $1 namespace $2 pod $3 container $4 file $5 output
copy_rrc() {
  kubectl -n "$1" exec "$2" -c "$3" -- sh -c '[ -r "$0" ] && cat "$0"' "$4" > "$5.tmp" 2>> "$D/err" \
    && mv "$5.tmp" "$5"
}

# $1 component $2 namespace $3 pod $4 output: nrRRC_stats.log, from the container and file of the last success first
read_oai() {
  if [ -f "$4.src" ]; then
    set -- "$1" "$2" "$3" "$4" $(cat "$4.src")
    if [ "$5" = "$3" ] && copy_rrc "$2" "$3" "$6" "$7" "$4"; then return 0; fi
  fi
  ordered=""
  for c in $(kubectl -n "$2" get "$3" -o jsonpath='{.spec.containers[*].name}' 2>> "$D/err"); do
    if [ "$c" = metrics-parser ]; then ordered="$c $ordered"; else ordered="$ordered $c"; fi
  done
  for c in $ordered; do
    for f in $OAI_FILES; do
      if copy_rrc "$2" "$3" "$c" "$f" "$4"; then echo "$3 $c $f" > "$4.src"; return 0; fi
    done
  done
  : > "$4"
  echo "$1: no readable nrRRC_stats.log in $3 ($OAI_FILES)" >> "$D/err"
}

while true; do
  : > "$D/err"
  files=""
  : > "$D/node_ips"
  while read -r comp ns selector type; do
    case "$comp" in "" | \#*) continue ;; esac
    out="$D/ran/$comp@$type.txt"
    pod=$(first_pod "$ns" "$selector")
    if [ -z "$pod" ]; then
      : > "$out"; echo "$comp: no running pod for $selector in $ns" >> "$D/err"
    elif [ "$type" = ocudu ]; then
      incremental_log "$ns" "$pod" "" "$out" "$OCUDU_PATTERN"
    else
      read_oai "$comp" "$ns" "$pod" "$out"
    fi
    [ -n "$pod" ] && node_ips "$comp" "$ns" "$pod" >> "$D/node_ips"
    files="$files $out"
  done < /rnti-mapper/sources

  amf=$(first_pod "${CORE_NS}" "${AMF_SELECTOR}")
  if [ -z "$amf" ]; then
    : > "$D/core.log"; echo "no running AMF for ${AMF_SELECTOR} in ${CORE_NS}" >> "$D/err"
  elif [ "${CORE_TYPE}" = open5gs ]; then
    incremental_log "${CORE_NS}" "$amf" "${AMF_CONTAINER}" "$D/core.log" "$OPEN5GS_PATTERN"
  else
    kubectl -n "${CORE_NS}" logs "$amf" -c "${AMF_CONTAINER}" --tail=3000 > "$D/core.log" 2>> "$D/err"
  fi

  : > "$D/inventory.json"
  [ -n "${INVENTORY_URL}" ] && wget -q -T 5 -O "$D/inventory.json" "${INVENTORY_URL}" 2>> "$D/err"

  # $files is a list of paths without spaces (component names are Kubernetes-safe)
  # shellcheck disable=SC2086
  awk -v core="${CORE_TYPE}" -f /rnti-mapper/join.awk \
    /rnti-mapper/ue_names "$D/core.log" "$D/inventory.json" "$D/node_ips" $files > /www/metrics.tmp \
    && mv /www/metrics.tmp /www/metrics

  cmp -s "$D/err" "$D/err.last" 2> /dev/null || cat "$D/err"
  mv "$D/err" "$D/err.last"
  sleep "${PERIOD}"
done
