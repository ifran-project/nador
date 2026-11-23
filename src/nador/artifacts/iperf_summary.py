"""Throughput summary of the iperf3 reports saved by an ExperimentRun."""

import json
from pathlib import Path


def _iperf_reports(output_directory: Path) -> dict[str, dict]:
    """iperf3 reports (TCP, iperf3 -J) and failed steps ({"error": ...}) saved at the top of the run directory."""
    reports = {}
    for path in sorted(Path(output_directory).glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        end = data.get("end")
        if (isinstance(end, dict) and "sum_sent" in end) or set(data) == {"error"}:
            reports[path.stem] = data
    return reports


def iperf_summary(output_directory: Path) -> str:
    """
    One line per iperf step of a run: sent and received throughput, TCP retransmissions, or the error.

    Steps follow the order of scenario.json when the run saved one (list of steps with a "name"), the other reports
    come after in alphabetical order.
    """
    reports = _iperf_reports(output_directory)
    order = []
    scenario_path = Path(output_directory) / "scenario.json"
    if scenario_path.exists():
        scenario = json.loads(scenario_path.read_text())
        if isinstance(scenario, list):
            order = [step["name"] for step in scenario if isinstance(step, dict) and step.get("name") in reports]
    order += [name for name in reports if name not in order]
    if not order:
        return f"No iperf report in {output_directory}"
    width = max(len(name) for name in order)
    lines = []
    for name in order:
        report = reports[name]
        if "error" in report and "end" not in report:
            lines.append(f"{name:{width}s}  ERROR {report['error']}")
            continue
        sent, received = report["end"]["sum_sent"], report["end"].get("sum_received", {})
        received_mbps = f"{received['bits_per_second'] / 1e6:7.1f}" if "bits_per_second" in received else "      -"
        lines.append(
            f"{name:{width}s}  sent {sent['bits_per_second'] / 1e6:7.1f} Mbps  received {received_mbps} Mbps  "
            f"retransmits {sent.get('retransmits', '-')}"
        )
    return "\n".join(lines)
