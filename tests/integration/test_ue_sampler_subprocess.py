"""The UE sampler, run as the subprocess ExperimentRun starts, against a local mapper API."""

import asyncio
import http.server
import json
import threading

import pytest

from nador.artifacts import ExperimentRun

pytestmark = pytest.mark.integration


async def test_the_ue_sampler_runs_as_a_subprocess_against_the_mapper(tmp_path):
    class Mapper(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"ues": [{"imsi": "001010000000001"}], "count": 1, "limit": 5000}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mapper)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        async with ExperimentRun(tmp_path / "run", mapper_url=url, interval_seconds=0.2, timezone="UTC"):
            await asyncio.sleep(0.6)
    finally:
        server.shutdown()
        server.server_close()

    output = tmp_path / "run" / "ue_context"
    assert json.loads((output / "collection_status.json").read_text())["status"] == "completed"
    summary = json.loads((output / "summary.json").read_text())
    assert summary["samples"] >= 2
    assert summary["errors"] == 0
    assert summary["stop_reason"] == "requested"
