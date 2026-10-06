"""Newline-delimited JSON over a connected socket.

One request and one response per connection. This is the only module that
knows the bytes on the wire.
"""

import json
import socket

from kiln.errors import KilnError

PING = "ping"
START_FACTORY = "start_factory"
FACTORY_STATUS = "factory_status"
EVENTS = "events"
SERVER_STATUS = "server_status"
STOP_SERVER = "stop_server"

_MAX_BYTES = 1_000_000


def ok(**fields: object) -> dict:
    return {"ok": True, **fields}


def error(message: str) -> dict:
    return {"ok": False, "error": message}


def send(sock: socket.socket, message: dict) -> None:
    payload = json.dumps(message).encode() + b"\n"
    if len(payload) > _MAX_BYTES:
        raise KilnError("message too large")
    sock.sendall(payload)


def recv(sock: socket.socket) -> dict:
    chunks: list[bytes] = []
    size = 0
    while True:
        try:
            chunk = sock.recv(4096)
        except TimeoutError as exc:
            raise KilnError("kiln server is not running") from exc
        if not chunk:
            raise KilnError("kiln server closed the connection")
        chunks.append(chunk)
        size += len(chunk)
        if size > _MAX_BYTES:
            raise KilnError("message too large")
        if b"\n" in chunk:
            break
    line = b"".join(chunks).split(b"\n", 1)[0]
    try:
        message = json.loads(line.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KilnError("malformed message") from exc
    if not isinstance(message, dict):
        raise KilnError("message must be an object")
    return message
