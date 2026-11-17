"""Subscribe to OCUDU JSON metrics and stream them to Telegraf execd."""

import json
import logging
import os
import time

import websocket


def emit(value):
    """Keep stdout exclusively for newline-delimited JSON."""
    print(json.dumps(value), flush=True)


def forward(message):
    """Ignore control acknowledgements and reject malformed/non-object payloads."""
    value = json.loads(message)
    if not isinstance(value, dict) or "cmd" in value:
        return False
    if not any(key != "timestamp" for key in value):
        return False
    emit(value)
    return True


def run():
    """Reconnect after both clean closes and errors; report stream freshness."""
    logging.basicConfig(level=logging.INFO)
    received = errors = 0
    last_message = 0

    def health(connected):
        emit({"internal_": {
            "connected": int(connected),
            "messages_total": received,
            "decode_errors_total": errors,
            "last_message_timestamp_seconds": last_message,
        }})

    while True:
        connection = None
        try:
            connection = websocket.create_connection(os.environ["WS_URL"], timeout=5, enable_multithread=False)
            connection.send(json.dumps({"cmd": "metrics_subscribe"}))
            health(True)
            while True:
                try:
                    message = connection.recv()
                except websocket.WebSocketTimeoutException:
                    health(True)
                    continue
                if not message:
                    break
                try:
                    if forward(message):
                        received += 1
                        last_message = time.time()
                except (ValueError, UnicodeError):
                    errors += 1
                health(True)
        except (OSError, websocket.WebSocketException) as exc:
            logging.warning("OCUDU metrics connection failed: %s", exc)
        finally:
            if connection is not None:
                connection.close()
            health(False)
        time.sleep(2)


if __name__ == "__main__":
    run()
