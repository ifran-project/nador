"""Request probe lifecycle changes from the Nador controller HTTP API."""

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward the controller bearer token to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    """List, start, or stop probes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("NADOR_CONTROLLER_URL", "http://127.0.0.1:8787"))
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    start = actions.add_parser("start")
    start.add_argument("probe")
    start.add_argument("--imsi", action="append")
    start.add_argument("--slice", action="append")
    start.add_argument("--all", action="store_true", help="Clear both existing UE filters")
    start.add_argument("--log-samples", action=argparse.BooleanOptionalAction, default=None)
    actions.add_parser("stop").add_argument("probe")
    args = parser.parse_args()
    url = urllib.parse.urlsplit(args.url)
    try:
        loopback = ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        loopback = url.hostname == "localhost"
    if (
        not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or (url.scheme != "https" and not (url.scheme == "http" and loopback))
    ):
        parser.error("Use HTTPS for remote controllers; HTTP is only allowed on loopback")
    token = os.getenv("NADOR_CONTROLLER_TOKEN")
    if url.scheme == "https" and not token:
        parser.error("Set NADOR_CONTROLLER_TOKEN for the HTTPS endpoint")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    endpoint = args.url.rstrip("/") + "/v1/probes"
    data = None
    if args.action != "list":
        endpoint += "/" + urllib.parse.quote(args.probe, safe="")
        payload = {"enabled": args.action == "start"}
        if args.action == "start":
            if args.all and (args.imsi or args.slice):
                parser.error("--all cannot be combined with --imsi or --slice")
            if args.imsi is not None or args.all:
                payload["imsis"] = args.imsi or []
            if args.slice is not None or args.all:
                payload["slices"] = args.slice or []
            if args.log_samples is not None:
                payload["log_samples"] = args.log_samples
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="GET" if data is None else "PUT")
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
            print(json.dumps(json.load(response), indent=2))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read(8192).decode(errors='replace')}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Controller request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
