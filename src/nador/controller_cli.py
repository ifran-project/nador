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

    def redirect_request(self, *_args: object) -> None:
        """Refuse every redirect."""


def _parser() -> argparse.ArgumentParser:
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
    return parser


def _is_safe_endpoint(url: urllib.parse.SplitResult) -> bool:
    """HTTPS anywhere, plain HTTP only on loopback, and no credentials, query or fragment."""
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        return False
    try:
        loopback = ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        loopback = url.hostname == "localhost"
    return url.scheme == "https" or (url.scheme == "http" and loopback)


def _payload(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict:
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
    return payload


def main() -> int:
    """List, start, or stop probes."""
    parser = _parser()
    args = parser.parse_args()
    url = urllib.parse.urlsplit(args.url)
    if not _is_safe_endpoint(url):
        parser.error("Use HTTPS for remote controllers; HTTP is only allowed on loopback")
    token = os.getenv("NADOR_CONTROLLER_TOKEN")
    if url.scheme == "https" and not token:
        parser.error("Set NADOR_CONTROLLER_TOKEN for the HTTPS endpoint")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    endpoint = args.url.rstrip("/") + "/v1/probes"
    data = None
    if args.action != "list":
        endpoint += "/" + urllib.parse.quote(args.probe, safe="")
        data = json.dumps(_payload(args, parser)).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="GET" if data is None else "PUT")  # noqa: S310 - scheme checked above
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
