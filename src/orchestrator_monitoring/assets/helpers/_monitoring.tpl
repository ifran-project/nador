{{- define "monitoring.labels" -}}
monitoring.blueprint/owner: {{ .Release.Namespace | quote }}
app.kubernetes.io/instance: {{ .Release.Name | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service | quote }}
{{- end }}

{{- define "monitoring.placement" -}}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}

{{- define "monitoring.controllerEnv" -}}
{{- $throughput := eq (.Values.probeKind | default "latency") "throughput" }}
- name: MONITORING_NS
  value: {{ .Release.Namespace | quote }}
- name: CORE_NS
  value: {{ .Release.Namespace | quote }}
- name: WATCH_NAMESPACES
  value: {{ join "," .Values.controller.watchNamespaces | quote }}
- name: PROBE_MODE
  value: {{ ternary "upf" .Values.controller.probeMode $throughput | quote }}
- name: PROBE_NAME_PREFIX
  value: {{ ternary "throughput-probe" "latency-probe" $throughput | quote }}
- name: PROBE_KIND
  value: {{ ternary "throughput" "latency" $throughput | quote }}
- name: BASE_HANDLE
  value: {{ ternary "1000" "1" $throughput | quote }}
- name: BASE_PRIO
  value: {{ ternary "1000" "1" $throughput | quote }}
- name: BASE_EXPORTER_PORT
  value: {{ ternary "9300" "9100" $throughput | quote }}
- name: EXPORTER_PATH
  value: {{ ternary "/metrics" "/latency" $throughput | quote }}
- name: UE_MAPPER_URL
  value: {{ printf "http://ue-mapper-api.%s.svc.cluster.local:8080" .Release.Namespace | quote }}
- name: PROBE_IMAGE
  value: {{ ternary .Values.throughput.probeImage .Values.images.probe $throughput | quote }}
- name: PACKET_PAIRING
  value: {{ ternary "1" "0" (and (not $throughput) .Values.latency.packetPairing) | quote }}
- name: PACKET_PAIRER_IMAGE
  value: {{ .Values.images.pairer | quote }}
- name: PACKET_PAIRER_NAME
  value: packet-pairer
- name: PACKET_PAIRER_SERVICE_NAME
  value: packet-pairer
- name: PACKET_PAIRER_NAMESPACE
  value: {{ .Release.Namespace | quote }}
- name: PACKET_PAIRER_PORT
  value: "9201"
- name: PROBE_IFACE
  value: {{ .Values.controller.gnbInterface | quote }}
- name: UPF_PROBE_IFACE
  value: {{ .Values.controller.upfInterface | quote }}
- name: GNB_LATENCY_MODE
  value: {{ .Values.controller.gnbLatencyMode | quote }}
- name: UPF_LATENCY_MODE
  value: {{ .Values.controller.upfLatencyMode | quote }}
{{- end }}
