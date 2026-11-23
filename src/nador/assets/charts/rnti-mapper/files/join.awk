# Join the RAN view (RNTI <-> RAN UE NGAP id, per RRC node) with the core view (RAN UE NGAP id <-> IMSI) and the
# UE names, and print the Prometheus info metric
#   ran_ue_info{ran_node,ran_component,rnti,ran_ue_id,imsi,ue_name} 1
# ran_node is the gNB / CU / CU-CP whose RRC holds the UE context; ran_component is the cell's gNB or DU (the DU name
# is the nador component name), as in the service label metrics-ran-<component> of the OAI gNB/DU metrics.
# rnti is 4-digit lowercase hex, as written by OAI ("RNTI %04x") and shown by OCUDU ("c-rnti=0x...").
#
# Usage: awk -v core=oai|open5gs -f join.awk ue_names core.log inventory.json node_ips <dir>/<component>@<oai|ocudu>.txt ...
#   ue_names       "IMSI name" lines ('#' comments)
#   core.log       OAI AMF log ("UEs' Information" tables) or Open5GS AMF attach lines
#   inventory.json ue-mapper-api /inventory/ues (may be empty): IMSI per (gnb_id = gNB N3 IP, RAN UE NGAP id), exact
#   node_ips       "<component> <ip> ..." lines: IPs of each RRC node pod (pod network and Multus)
#   *@oai.txt      OAI nrRRC_stats.log of a gNB / CU / CU-CP
#   *@ocudu.txt    OCUDU gNB/CU log lines "c-rnti=" and "ran_ue=" (CU-CP and NGAP at info level)
#
# RAN UE NGAP ids are allocated by each NG node (gNB, CU) independently. An inventory row whose gnb_id is an IP of an
# RRC node binds the IMSI to that node exactly. Otherwise (no inventory, N3 IP not on the RRC node pod) the core view
# is keyed by the id alone, and an id used by several RRC nodes at once is reported as imsi="ambiguous".
# Written for busybox awk (no gawk extensions).

function trim(s) { gsub(/^[ \t]+|[ \t]+$/, "", s); return s }

# "0x1f" or "31" -> 31
function todec(s,   i, c, v, h) {
  s = tolower(trim(s))
  if (substr(s, 1, 2) != "0x") return s + 0
  h = "0123456789abcdef"; v = 0
  for (i = 3; i <= length(s); i++) { c = index(h, substr(s, i, 1)); if (c == 0) break; v = v * 16 + c - 1 }
  return v
}

# "0x4601" / "4601" -> "4601" (4-digit lowercase hex)
function hex4(s) {
  s = tolower(trim(s)); sub(/^0x/, "", s)
  while (length(s) < 4) s = "0" s
  return s
}

function bind(id, im) { imsi_of[id] = im; delete amb[id] }

FILENAME == ARGV[1] { if ($1 !~ /^#/ && NF >= 2) name[$1] = $2; next }

FILENAME == ARGV[2] {
  gsub(/\033\[[0-9;]*m/, "")          # strip ANSI colors (Open5GS)
  if (core == "open5gs") { o5gs_line(); next }
  # OAI AMF "UEs' Information" table. Only the last table is kept.
  if (index($0, "UEs' Information")) { split("", imsi_of); split("", reg); next }
  n = split($0, f, "|")
  if (n >= 9 && trim(f[2]) ~ /^[0-9]+$/ && f[3] ~ /5GMM/) {     # data rows only (not the header)
    id = todec(f[6]); st = trim(f[3])
    # a registered UE wins over a stale (deregistered) entry with the same RAN UE NGAP id
    if (!(id in imsi_of) || (st == "5GMM-REGISTERED" && !reg[id])) {
      imsi_of[id] = trim(f[4]); reg[id] = (st == "5GMM-REGISTERED")
    }
  }
  next
}

# Open5GS AMF (single event loop). The InitialUEMessage handler prints, in one event:
#   "    RAN_UE_NGAP_ID[n] AMF_UE_NGAP_ID[m] ..." then "[imsi-X] Known UE by ..." (exact)
#   or "Unknown UE by ...": the identity then comes in later events ("[suci-...] SUCI",
#   "[imsi-X] Registration complete") that carry no NGAP id. Such an identity is bound only
#   if a single attach is pending; with overlapping attaches the ids are marked ambiguous
#   (the inventory, when present, resolves them exactly).
function o5gs_line(   im, p, k) {
  if (index($0, "InitialUEMessage")) { want = 1; return }
  if (want && match($0, /RAN_UE_NGAP_ID\[[0-9]+\]/)) {
    cur = substr($0, RSTART + 15, RLENGTH - 16) + 0; want = 0; return
  }
  if (cur != "") {
    if (index($0, "Known UE by") && !index($0, "Unknown UE by") && match($0, /\[imsi-[0-9]+\]/)) {
      bind(cur, substr($0, RSTART + 6, RLENGTH - 7))
    } else { pend[cur] = 1; npend++ }
    cur = ""; return
  }
  if (npend == 0) return
  im = ""
  if (match($0, /\[suci-0-[0-9]+-[0-9]+-[0-9]+-0-[0-9]+-[0-9]+\]/)) {
    split(substr($0, RSTART + 1, RLENGTH - 2), p, "-")   # suci 0 mcc mnc routing scheme key msin
    im = p[3] p[4] p[8]
  } else if (index($0, "Registration complete") && match($0, /\[imsi-[0-9]+\]/))
    im = substr($0, RSTART + 6, RLENGTH - 7)
  if (im == "") return
  if (npend == 1) { for (k in pend) bind(k, im) }
  else { for (k in pend) { amb[k] = 1; delete imsi_of[k] } }
  split("", pend); npend = 0
}

# ue-mapper-api inventory: {"ues":[{"imsi":"...","ran_ue_id":"0","gnb_id":"10.244.1.2",...},...]}, read whole
FILENAME == ARGV[3] { buf = buf $0; next }

# RRC node pod IPs
FILENAME == ARGV[4] { for (i = 2; i <= NF; i++) node_of_ip[$i] = $1; next }

function load_inventory(   n, i, o, id, im, ip) {
  if (loaded) return
  loaded = 1
  n = split(buf, o, "}")
  for (i = 1; i <= n; i++) {
    id = ""; im = ""
    if (match(o[i], /"ran_ue_id": *"?[0-9]+/)) { id = substr(o[i], RSTART, RLENGTH); gsub(/[^0-9]/, "", id) }
    if (match(o[i], /"imsi": *"[0-9]+"/)) { im = substr(o[i], RSTART + 7, RLENGTH - 7); gsub(/[^0-9]/, "", im) }
    if (id == "" || im == "") continue
    id += 0
    ip = ""
    if (match(o[i], /"gnb_id": *"[0-9.]+"/)) { ip = substr(o[i], RSTART, RLENGTH); sub(/^"gnb_id": *"/, "", ip); sub(/"$/, "", ip) }
    if (ip in node_of_ip) scoped[node_of_ip[ip] SUBSEP id] = im     # exact: this RRC node, this id
    # one id with two identities in the inventory (two NG nodes): not resolvable from the id alone
    if ((id in inv) && inv[id] != im) { amb[id] = 1; delete imsi_of[id]; inv_amb[id] = 1; continue }
    inv[id] = im
    if (!(id in inv_amb)) bind(id, im)
  }
}

# RAN files: <component>@<type>.txt
FNR == 1 && FILENAME != ARGV[1] && FILENAME != ARGV[2] && FILENAME != ARGV[3] && FILENAME != ARGV[4] {
  src = FILENAME; sub(/^.*\//, "", src); sub(/\.txt$/, "", src)
  split(src, sp, "@"); node = sp[1]; type = sp[2]; cur_ue = ""
}

# OAI nrRRC_stats.log:
#   "UE 0 CU UE ID 1 DU UE ID 19420 RNTI 4bdc random identity ...:"   (CU UE ID = RAN UE NGAP id)
#   "    associated DU:  (local/integrated CU-DU)" | " DU assoc ID 7" | " DU offline/unavailable"
#   "[1] DU ID 3584 (du1) assoc_id 7: 1 cell"                           (after the UE list)
type == "oai" && /^UE [0-9]+ CU UE ID / {
  cu = ""; rnti = ""
  for (i = 1; i <= NF; i++) {
    if ($i == "CU" && $(i + 1) == "UE" && $(i + 2) == "ID") cu = $(i + 3)
    if ($i == "RNTI") rnti = $(i + 1)
  }
  if (cu == "" || rnti == "") next
  cur_ue = ++nrow; r_node[nrow] = node; r_rnti[nrow] = hex4(rnti); r_id[nrow] = cu + 0
  next
}
type == "oai" && cur_ue != "" && /^ *associated DU:/ {
  if (match($0, /DU assoc ID -?[0-9]+/)) r_assoc[cur_ue] = substr($0, RSTART + 12, RLENGTH - 12) + 0   # after "DU assoc ID "
  next
}
type == "oai" && /^\[[0-9]+\] DU ID [0-9]+ \(/ {
  if (match($0, /\([^)]*\) assoc_id -?[0-9]+/)) {
    s = substr($0, RSTART + 1, RLENGTH - 1)            # "du1) assoc_id 7"
    split(s, q, /\) assoc_id /); du_name[node SUBSEP (q[2] + 0)] = q[1]
  }
  next
}

# OCUDU gNB/CU log, CU-CP index "ue=N" in both:
#   [CU-CP] ue=0 c-rnti=0x4601: UE created      [RRC] ue=0 c-rnti=0x4601: ...
#   [NGAP] Tx PDU ue=0 ran_ue=0: InitialUEMessage
# A "UE created" line starts a new context on that index (the index is reused).
type == "ocudu" {
  if (match($0, /ue=[0-9]+ c-rnti=0x[0-9a-fA-F]+/)) {
    split(substr($0, RSTART, RLENGTH), q, /[= ]/)          # ue N c-rnti 0xHHHH
    k = node SUBSEP q[2]
    if (index($0, "UE created") || !(k in crnti)) { if (!(k in seen_k)) { seen_k[k] = 1; ok_list[++nk] = k }; delete ranid[k] }
    crnti[k] = hex4(q[4])
  } else if (match($0, /ue=[0-9]+ ran_ue=[0-9]+/)) {
    split(substr($0, RSTART, RLENGTH), q, /[= ]/)          # ue N ran_ue M
    ranid[node SUBSEP q[2]] = q[4] + 0
  }
  next
}

function ue_name(im,   k, hit, n) {
  if (im in name) return name[im]
  if (im ~ /^[0-9]+$/ && length(im) <= 10) {               # MSIN only (from a SUCI): unique IMSI with this suffix
    for (k in name) if (substr(k, length(k) - length(im) + 1) == im) { hit = name[k]; n++ }
    if (n == 1) return hit
  }
  return im
}

END {
  load_inventory()
  # OCUDU contexts with a known NGAP id, one row per live CU-CP index
  for (i = 1; i <= nk; i++) {
    k = ok_list[i]
    if (!(k in ranid)) continue
    split(k, q, SUBSEP)
    ++nrow; r_node[nrow] = q[1]; r_rnti[nrow] = crnti[k]; r_id[nrow] = ranid[k]
  }
  # RAN UE NGAP ids used by more than one RRC node
  for (i = 1; i <= nrow; i++) {
    k = r_id[i] SUBSEP r_node[i]
    if (!(k in id_node)) { id_node[k] = 1; nodes_of[r_id[i]]++ }
  }
  print "# HELP ran_ue_info RNTI to UE identity mapping (value is always 1)"
  print "# TYPE ran_ue_info gauge"
  for (i = 1; i <= nrow; i++) {
    id = r_id[i]
    if ((r_node[i] SUBSEP id) in scoped) im = scoped[r_node[i] SUBSEP id]
    else if (nodes_of[id] > 1 || (id in amb)) im = "ambiguous"
    else if (id in imsi_of) im = imsi_of[id]
    else im = "unknown"
    comp = r_node[i]
    if ((i in r_assoc) && ((r_node[i] SUBSEP r_assoc[i]) in du_name)) comp = du_name[r_node[i] SUBSEP r_assoc[i]]
    printf "ran_ue_info{ran_node=\"%s\",ran_component=\"%s\",rnti=\"%s\",ran_ue_id=\"%s\",imsi=\"%s\",ue_name=\"%s\"} 1\n", \
      r_node[i], comp, r_rnti[i], id, im, ue_name(im)
  }
}
