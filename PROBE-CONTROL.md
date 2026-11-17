# External RTT and on-demand probes

Enable external RTT in your monitoring profile:

```yaml
latency: true
packet_pairing: false
rtt_imsis: []
rtt_slices: ["01:ffffff"]
```

RTT uses disposable external probe pods only. Enabling `latency` selects the
pinned external controller unless you explicitly supply your own image.
Old profiles with `rtt_placement: injected` are rejected; remove that setting.
The external controller currently supports one gNB and one UPF target.
Multi-anchor examples keep their other metrics but leave RTT disabled.

An empty selector list does not restrict that dimension; specifying
both selects the intersection. The controller starts one external RTT pod per
configured gNB/UPF anchor with matching UE sessions and removes it when
disabled or when there are no matching sessions. AMF/SMF sniffers and
the mapper remain the session context infrastructure. Same-packet pairing is
not supported by this external controller.

With `probe_controller_image` set, the existing monitoring-controller
deployment also owns the N3 and UPF transit workloads selected under
`one_way`; it does not install the separate one-way release. Do not
switch from injected to external on the same target pods: recreate the
gNB and UPF pods first so their ephemeral containers and filters are gone.
If the separate one-way Helm release was previously installed, remove
it before handing the ConfigMap and Service over to the external controller.

Use the API below to change probe state at runtime. IMSI and slice selectors
apply to external RTT and throughput. One-way metrics carry slice labels but
are not filtered by these selectors. You can also use the Python
`monitoring.set_one_way()` method. A Helm upgrade restores the YAML settings.

## HTTP API

By default the API listens on loopback. Access it through Kubernetes
port forwarding, which requires pod port-forward access:

```sh
kubectl -n monitoring port-forward deployment/monitoring-controller 8787:8787
```

In a second terminal:

```sh
curl -fsS http://127.0.0.1:8787/v1/probes
curl -fsS -X PUT http://127.0.0.1:8787/v1/probes/rtt \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true,"imsis":[],"slices":["01:ffffff"]}'
curl -fsS -X PUT http://127.0.0.1:8787/v1/probes/cpu \
  -H 'Content-Type: application/json' -d '{"enabled":false}'
```

The response confirms the desired state was saved, not that a workload
is ready. Check the pods or Prometheus for actual readiness.

In addition to `rtt`, `n3`, and `upf_transit`, the API controls
`cpu`, `drops`, `queueing`, `tcp`, `split-signaling`, and
`measurement-quality` when pinned under `extended.probe_images`.
Their YAML flags select startup state and feature settings. An initially
disabled dedicated release starts no pods and can be enabled later.
For a wholly disabled drops or TCP release, starting it enables only
general drop reasons or socket counters, respectively. Configure
attributed drops or TCP flow evidence in YAML when those are needed.
Split signaling is only available for a modeled split.

In controller mode, `throughput` and `user_plane_paths` also run as
disposable pods. Throughput accepts IMSI/slice selectors like RTT.
Path counters cover interfaces, not individual UEs. AMF/SMF sniffers,
mapper and node exporters remain monitoring infrastructure.

## Public HTTPS

Create a Kubernetes TLS Secret with `tls.crt` and `tls.key` for the
API hostname and another Secret with a random bearer token under
`token` (at least 32 ASCII characters). Set only their names in YAML:

```yaml
probe_api_tls_secret: nador-controller-tls
probe_api_token_secret: nador-controller-token
```

This creates a LoadBalancer Service on port 443. The cluster must
provide a load balancer and your DNS name must point at it. Restrict
network access to trusted clients. Without both settings the API
remains on loopback. The controller reads the credentials at startup;
restart its deployment after changing them. Do not expose the loopback
endpoint or share the bearer token with untrusted clients.

After reinstalling Nador in your environment, use:

```sh
export NADOR_CONTROLLER_URL=https://your-controller.example.org
# Set NADOR_CONTROLLER_TOKEN in the environment from your secret store.
nador-controller list
nador-controller start rtt --slice 01:ffffff
nador-controller start throughput --imsi 001010000000100
nador-controller stop rtt
```

If a filter was previously set, an omitted selector keeps it. Use
`--all` to deliberately clear both selectors. The command only accepts
probe operations, not shell commands. HTTP 202 means the request was
saved; confirm actual workload readiness separately.
