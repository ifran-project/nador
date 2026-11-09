"""Read-only validation helpers for OAI parser metrics."""

import math


def validate_oai_goodput(goodput, *, namespace, component):
    """Require finite nonnegative DL/UL goodput for the same radio-local RNTIs."""
    radio_ids = {}
    for direction in ("dl", "ul"):
        samples = goodput.get(direction, [])
        if not samples or any(
            not math.isfinite(float(row["value"][1])) or float(row["value"][1]) < 0
            or not row["metric"].get("rnti")
            or row["metric"].get("namespace") != namespace
            for row in samples
        ):
            raise AssertionError(f"{component}: invalid native MAC goodput ({direction})")
        radio_ids[direction] = {row["metric"]["rnti"] for row in samples}
    if radio_ids["dl"] != radio_ids["ul"]:
        raise AssertionError(f"{component}: uplink/downlink goodput covers different RNTIs")
    return goodput
