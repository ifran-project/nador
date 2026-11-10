"""Render or create a bounded, passive external NGAP visibility check."""

import argparse
import json
import re
import subprocess
from uuid import uuid4


def job_for_pod(pod, *, image, interface, seconds, namespace, name):
    """Use the live pod's UID and node; no cluster API token in the probe."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface) or not 5 <= seconds <= 120:
        raise ValueError("Supply an explicit Linux interface and 5-120 seconds")
    if not image or image.endswith(":latest") or ":" not in image.rsplit("/", 1)[-1]:
        raise ValueError("Supply an explicit published visibility image tag or digest")
    if pod["status"]["phase"] != "Running" or pod["metadata"].get("deletionTimestamp"):
        raise ValueError("The selected AMF pod must be running and not terminating")
    node, uid = pod["spec"]["nodeName"], pod["metadata"]["uid"]
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": seconds + 90,
                 "ttlSecondsAfterFinished": 600,
                 "template": {"spec": {
                     "nodeName": node, "hostPID": True, "automountServiceAccountToken": False,
                     "restartPolicy": "Never", "tolerations": [{"operator": "Exists"}],
                     "containers": [{"name": "visibility", "image": image,
                                     "securityContext": {"privileged": True},
                                     "args": ["--pod-uid", uid, "--interface", interface, "--seconds", str(seconds)],
                                     "volumeMounts": [{"name": "proc", "mountPath": "/host/proc", "readOnly": True}]}],
                     "volumes": [{"name": "proc", "hostPath": {"path": "/proc", "type": "Directory"}}],
                 }}},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("context", "amf-namespace", "amf-pod", "interface", "image"):
        parser.add_argument("--" + option, required=True)
    parser.add_argument("--namespace", default="monitoring")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--create", action="store_true", help="Create only this temporary Job")
    args = parser.parse_args()
    kubectl = ["kubectl", "--context", args.context, "--request-timeout=15s"]
    pod = json.loads(subprocess.check_output(
        [*kubectl, "-n", args.amf_namespace, "get", "pod", args.amf_pod, "-o", "json"], timeout=20,
    ))
    job = job_for_pod(pod, image=args.image, interface=args.interface, seconds=args.seconds,
                      namespace=args.namespace, name="amf-visibility-" + uuid4().hex[:8])
    manifest = json.dumps(job, indent=2)
    if args.create:
        subprocess.run([*kubectl, "create", "-f", "-"], input=manifest, text=True, timeout=20, check=True)
    else:
        print(manifest)


if __name__ == "__main__":
    main()
