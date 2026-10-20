{{/* vim: set filetype=mustache: */}}
{{/*
Expand the name of the chart.
*/}}
{{- define "oai-cu_up.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "oai-cu_up.fullname" -}}
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
{{- define "oai-cu_up.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "oai-cu_up.labels" -}}
helm.sh/chart: {{ include "oai-cu_up.chart" . }}
{{ include "oai-cu_up.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "oai-cu_up.selectorLabels" -}}
app.kubernetes.io/name: {{ include "oai-cu_up.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Create the name of the service account to use
*/}}
{{- define "oai-cu_up.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
    {{ default (include "oai-cu_up.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
    {{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}

{{/*
Count enabled interfaces with defaultRoute defined and non-empty
*/}}
{{- define "oai-cu_up.countDefaultRouteInterfaces" -}}
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
Resolve F1u interface name
- Uses enabled: true check
- Falls back to other interfaces if F1u is disabled or missing
- Defaults to eth0
*/}}
{{- define "oai-cu_up.f1uIfName" -}}
{{- if not .Values.multus.enabled -}}
eth0
{{- else -}}
  {{- $f1u := "" -}}
  {{- $n3 := "" -}}
  {{- $e1 := "" -}}
  {{- range $if := .Values.multus.interfaces }}
    {{- if and (eq $if.name "f1u") $if.enabled }}{{- $f1u = $if.name -}}{{- end -}}
    {{- if and (eq $if.name "n3") $if.enabled }}{{- $n3 = $if.name -}}{{- end -}}
    {{- if and (eq $if.name "e1") $if.enabled }}{{- $e1 = $if.name -}}{{- end -}}
  {{- end -}}
  {{- if $f1u -}}
{{ $f1u }}
  {{- else if $n3 -}}
{{ $n3 }}
  {{- else if $e1 -}}
{{ $e1 }}
  {{- else -}}
eth0
  {{- end -}}
{{- end -}}
{{- end }}

{{/*
Resolve N2 interface name
- Uses enabled: true check
- Falls back to other interfaces if N2 is disabled or missing
- Defaults to eth0
*/}}
{{- define "oai-cu_up.n3IfName" -}}
{{- if not .Values.multus.enabled -}}
eth0
{{- else -}}
  {{- $n3 := "" -}}
  {{- $e1 := "" -}}
  {{- $f1u := "" -}}
  {{- range $if := .Values.multus.interfaces }}
    {{- if and (eq $if.name "n3") $if.enabled }}{{- $n3 = $if.name -}}{{- end -}}
    {{- if and (eq $if.name "e1") $if.enabled }}{{- $e1 = $if.name -}}{{- end -}}
    {{- if and (eq $if.name "f1u") $if.enabled }}{{- $f1u = $if.name -}}{{- end -}}
  {{- end -}}
  {{- if $n3 -}}
{{ $n3 }}
  {{- else if $f1u -}}
{{ $f1u }}
  {{- else if $e1 -}}
{{ $e1 }}
  {{- else -}}
eth0
  {{- end -}}
{{- end -}}
{{- end }}

{{/*
Resolve E1 interface name
- Uses enabled: true check
- Falls back to other interfaces if E1 is disabled or missing
- Defaults to eth0
*/}}
{{- define "oai-cu_up.e1IfName" -}}
{{- if not .Values.multus.enabled -}}
eth0
{{- else -}}
  {{- $e1 := "" -}}
  {{- $n3 := "" -}}
  {{- $f1u := "" -}}
  {{- range $if := .Values.multus.interfaces }}
    {{- if and (eq $if.name "e1") $if.enabled }}{{- $e1 = $if.name -}}{{- end -}}
    {{- if and (eq $if.name "n3") $if.enabled }}{{- $n3 = $if.name -}}{{- end -}}
    {{- if and (eq $if.name "f1u") $if.enabled }}{{- $f1u = $if.name -}}{{- end -}}
  {{- end -}}
  {{- if $e1 -}}
{{ $e1 }}
  {{- else if $f1u -}}
{{ $f1u }}
  {{- else if $n3 -}}
{{ $n3 }}
  {{- else -}}
eth0
  {{- end -}}
{{- end -}}
{{- end }}


{{/*
Resolve E2 interface name using `name` field
*/}}
{{- define "oai-cu_up.e2IfName" -}}
{{- if and .Values.multus.enabled (gt (len .Values.multus.interfaces) 3) (hasKey (index .Values.multus.interfaces 3) "name") -}}
{{ (index .Values.multus.interfaces 3).name }}
{{- else -}}
eth0
{{- end -}}
{{- end }}
