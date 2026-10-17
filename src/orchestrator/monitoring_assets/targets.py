"""Explicit packet targets shared by the controller and offline <YOUR_PASSWORD>s."""
import os
import time


def match_target(targets, namespace, labels):
    matches = [t for t in targets if t.get('role') and t['namespace'] == namespace
               and t['selector'] and all(labels.get(k) == v for k, v in t['selector'].items())]
    if len(matches) > 1:
        raise ValueError(f'Ambiguous monitoring targets in {namespace}: {labels}')
    return matches[0] if matches else None


def ensure_sniffer(api, pod, namespace, target, owner, logger):
    """Reconcile one sniffer, including pod restarts and terminated ephemerals."""
    import kubernetes
    role = target['role']
    prefix = f'post5g-{role}-sniffer-'
    statuses = {s.name: s for s in (pod.status.ephemeral_container_statuses or [])}
    existing = pod.spec.ephemeral_containers or []
    active = [c for c in existing if c.name.startswith(prefix)
              and (c.name not in statuses or not statuses[c.name].state.terminated)]
    if active:
        return
    if target['target_container'] not in [c.name for c in pod.spec.containers]:
        raise ValueError(f"Missing container {target['target_container']} in {pod.metadata.name}")
    used = {c.name for c in existing}
    i = 1
    while f'{prefix}{i}' in used:
        i += 1
    new = {'name': f'{prefix}{i}', 'image': os.environ[f'{role.upper()}_SNIFFER_IMAGE'],
           'targetContainerName': target['target_container'], 'imagePullPolicy': 'IfNotPresent',
           'env': [{'name': f'{role}_sniffer_iface', 'value': target['iface']},
                   {'name': 'REDIS_HOST', 'value': os.environ['REDIS_HOST']},
                   {'name': 'POST5G_SNIFFER_OWNER', 'value': owner}],
           'securityContext': {'privileged': True, 'runAsUser': 0}}
    serialized = kubernetes.client.ApiClient().sanitize_for_serialization(existing)
    patch_ephemeral(pod.metadata.name, namespace,
        {'spec': {'ephemeralContainers': serialized + [new]}})
    logger.info('Started %s on %s/%s', new['name'], namespace, pod.metadata.name)


def stop_sniffers(api, pod, namespace, owner):
    """Signal only processes carrying the monitoring owner's explicit marker."""
    from kubernetes.stream import stream
    command = '''import os, pathlib, signal
marker = ('POST5G_SNIFFER_OWNER=' + os.environ['POST5G_SNIFFER_OWNER']).encode()
for path in pathlib.Path('/proc').glob('[0-9]*/environ'):
    try:
        pid = int(path.parent.name)
        if pid != os.getpid() and marker in path.read_bytes().split(b'\\0'):
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, FileNotFoundError):
        pass
'''
    statuses = {s.name: s for s in (pod.status.ephemeral_container_statuses or [])}
    names = []
    for c in pod.spec.ephemeral_containers or []:
        if not c.name.startswith(('post5g-amf-sniffer-', 'post5g-smf-sniffer-')):
            continue
        env = {e.name: e.value for e in c.env or []}
        if env.get('POST5G_SNIFFER_OWNER') != owner:
            continue
        status = statuses.get(c.name)
        if status and status.state.terminated:
            continue
        if not status or not status.state.running:
            raise RuntimeError(f'Sniffer {c.name} is pending; cannot confirm safe cleanup')
        names.append(c.name)
        stream(api.connect_get_namespaced_pod_exec, pod.metadata.name, namespace,
               container=c.name, command=['python', '-c', command], stderr=True, stdout=True, tty=False)
    deadline = time.monotonic() + 30
    while names:
        current = api.read_namespaced_pod(pod.metadata.name, namespace)
        states = {s.name: s for s in current.status.ephemeral_container_statuses or []}
        names = [n for n in names if n not in states or not states[n].state.terminated]
        if names and time.monotonic() >= deadline:
            raise RuntimeError(f'Sniffers did not stop: {names}')
        if names:
            time.sleep(1)


def patch_ephemeral(name, namespace, body):
    """Patch the subresource directly; older kubectl rejects ephemeralcontainers."""
    import kubernetes
    response = kubernetes.client.ApiClient().call_api(
        '/api/v1/namespaces/{namespace}/pods/{name}/ephemeralcontainers', 'PATCH',
        path_params={'namespace': namespace, 'name': name}, body=body,
        header_params={'Content-Type': 'application/strategic-merge-patch+json',
                       'Accept': 'application/json'},
        auth_settings=['BearerToken'],
        _return_http_data_only=True, _preload_content=False,
    )

    response.close()


def claim_pod(api, pod, namespace, owner):
    """Prevent two monitoring instances from managing the same packet probes."""
    annotations = pod.metadata.annotations or {}
    current = annotations.get('post5g.blueprint/packet-owner')
    if current and current != owner:
        raise RuntimeError(f'{namespace}/{pod.metadata.name} is managed by {current}')
    if current != owner:
        api.patch_namespaced_pod(pod.metadata.name, namespace, {'metadata': {
            'resourceVersion': pod.metadata.resource_version,
            'annotations': {'post5g.blueprint/packet-owner': owner}}})
