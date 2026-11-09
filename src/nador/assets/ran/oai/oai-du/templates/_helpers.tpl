{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "oai-du.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "oai-du.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "oai-du.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "oai-du.labels" -}}
helm.sh/chart: {{ include "oai-du.chart" . }}
{{ include "oai-du.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "oai-du.selectorLabels" -}}
app.kubernetes.io/name: {{ include "oai-du.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Create the name of the service account to use
*/}}
{{- define "oai-du.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
    {{ default (include "oai-du.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
    {{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}

{{/*
Count enabled interfaces with defaultRoute defined and non-empty
*/}}
{{- define "oai-du.countDefaultRouteInterfaces" -}}
{{- $count := 0 -}}
{{- if and .Values.multus.enabled (gt (len .Values.multus.interfaces) 0) -}}
  {{- range $i, $if := .Values.multus.interfaces }}
    {{- if $if.enabled }}
      {{- if (not (empty $if.defaultRoute)) }}
        {{- $count = add $count 1 }}
      {{- end }}
    {{- end }}
  {{- end }}
{{- end }}
{{- $count -}}
{{- end }}

{{/*
Resolve F1C interface name
- Uses enabled: true check
- Falls back to other interfaces if F1C is disabled or missing
- Defaults to eth0
*/}}
{{- define "oai-du.f1cIfName" -}}
{{- if not .Values.multus.enabled -}}
eth0
{{- else -}}
  {{- $f1c := "" -}}
  {{- $n2 := "" -}}
  {{- $f1u := "" -}}
  {{- range $if := .Values.multus.interfaces }}
    {{- if and (has $if.name (list "f1c" "f1")) $if.enabled }}{{- $f1c = $if.name -}}{{- end -}}
    {{- if and (eq $if.name "f1u") $if.enabled }}{{- $f1u = $if.name -}}{{- end -}}
  {{- end -}}
  {{- if $f1c -}}
{{ $f1c }}
  {{- else if $f1u -}}
{{ $f1u }}
  {{- else -}}
eth0
  {{- end -}}
{{- end -}}
{{- end }}

{{/*
Resolve F1U interface name
- Uses enabled: true check
- Falls back to other interfaces if F1U is disabled or missing
- Defaults to eth0
*/}}
{{- define "oai-du.f1uIfName" -}}
{{- if not .Values.multus.enabled -}}
eth0
{{- else -}}
  {{- $f1u := "" -}}
  {{- $f1c := "" -}}
  {{- range $if := .Values.multus.interfaces }}
    {{- if and (eq $if.name "f1u") $if.enabled }}{{- $f1u = $if.name -}}{{- end -}}
    {{- if and (has $if.name (list "f1c" "f1")) $if.enabled }}{{- $f1c = $if.name -}}{{- end -}}
  {{- end -}}
  {{- if $f1u -}}
{{ $f1u }}
  {{- else if $f1c -}}
{{ $f1c }}
  {{- else -}}
eth0
  {{- end -}}
{{- end -}}
{{- end }}


{{/*
Resolve E2 interface name using `name` field
*/}}
{{- define "oai-du.e2IfName" -}}
{{- if and .Values.multus.enabled (gt (len .Values.multus.interfaces) 3) (hasKey (index .Values.multus.interfaces 3) "name") -}}
{{ (index .Values.multus.interfaces 3).name }}
{{- else -}}
eth0
{{- end -}}
{{- end }}
