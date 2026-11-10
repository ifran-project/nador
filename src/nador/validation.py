"""Read-only validation helpers for implementation-native RAN metrics."""

import math
import time


def validate_ocudu_stream(health, native, *, namespace, component, kind, now=None):
    """Require a fresh connected source and real native data after UE traffic."""
    now = time.time() if now is None else now
    for metric in ("connected", "messages_total", "last_message_timestamp_seconds"):
        rows = health.get(metric, [])
        if len(rows) != 1:
            raise AssertionError(f"{component}: missing or duplicate OCUDU collector {metric}")
        value = float(rows[0]["value"][1])
        if not math.isfinite(value):
            raise AssertionError(f"{component}: non-finite OCUDU collector {metric}")
        if metric == "connected" and value != 1:
            raise AssertionError(f"{component}: OCUDU stream is disconnected")
        if metric == "messages_total" and value < 1:
            raise AssertionError(f"{component}: no native OCUDU messages received")
        if metric == "last_message_timestamp_seconds" and not 0 <= now - value < 30:
            raise AssertionError(f"{component}: OCUDU stream is stale or its clock is ahead")
    for row in [r for rows in health.values() for r in rows] + native:
        labels = row["metric"]
        if labels.get("source_namespace") != namespace or labels.get("ran_component") != component:
            raise AssertionError(f"{component}: OCUDU source identity mismatch")
        if not math.isfinite(float(row["value"][1])):
            raise AssertionError(f"{component}: non-finite OCUDU native data")
    if not native:
        raise AssertionError(f"{component}: collector is alive but native OCUDU data is missing")
    if kind in {"gnb", "du"}:
        identities = {}
        for direction in ("dl", "ul"):
            rows = [r for r in native if r["metric"].get("__name__") == f"ue_{direction}_brate"]
            if not rows or any(float(r["value"][1]) < 0 or not r["metric"].get("rnti") for r in rows):
                raise AssertionError(f"{component}: native OCUDU {direction} UE bitrate is missing or invalid")
            identities[direction] = {
                tuple(r["metric"].get(key) for key in ("cell_index", "pci", "rnti")) for r in rows
            }
        if identities["dl"] != identities["ul"]:
            raise AssertionError(f"{component}: OCUDU UL/DL UE identities disagree")
    return {"health": health, "native_series": len(native)}


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
