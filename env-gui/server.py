#!/usr/bin/env python3
"""Local editor for .env.example.

Run from anywhere:
    python env-gui/server.py
"""

from __future__ import annotations

import argparse
import json
import re
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env.example"
HTML_PATH = Path(__file__).resolve().parent / "index.html"

KEYS = ("SDK_VERSION", "ROBOT_MODEL", "SIMULATOR_CPU", "SIMULATOR_MEMORY")
MODELS = {
    "M0609",
    "M0617",
    "M1013",
    "M1509",
    "A0509",
    "A0912",
    "H2017",
    "H2515",
    "E0509",
    "P3020",
}
KEY_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")
VALIDATORS = {
    "SDK_VERSION": re.compile(r"^sdk(?:[2-9]|[1-9]\d)$"),
    "ROBOT_MODEL": re.compile(r"^[A-Za-z][A-Za-z0-9]{3,15}$"),
    "SIMULATOR_CPU": re.compile(r"^(?:0\.[1-9]\d?|[1-9]\d{0,2}(?:\.\d{1,2})?)$"),
    "SIMULATOR_MEMORY": re.compile(r"^\d+(?:\.\d+)?[kKmMgG](?:[iI]?[bB])?$"),
}


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = KEY_LINE.match(line)
        if not match:
            continue
        key = match.group(2)
        if key.startswith("#"):
            continue
        values[key] = match.group(4).strip()
    return values


def update_env(text: str, updates: dict[str, str]) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    ends_with_newline = text.endswith("\n")
    lines = text.splitlines()
    seen: set[str] = set()
    updated: list[str] = []

    for line in lines:
        match = KEY_LINE.match(line)
        if match and match.group(2) in updates:
            key = match.group(2)
            seen.add(key)
            updated.append(f"{match.group(1)}{key}{match.group(3)}{updates[key]}")
        else:
            updated.append(line)

    missing = [key for key in KEYS if key in updates and key not in seen]
    if missing and updated and updated[-1].strip():
        updated.append("")
    for key in missing:
        updated.append(f"{key}={updates[key]}")

    result = newline.join(updated)
    if ends_with_newline:
        result += newline
    return result


def validate(updates: dict[str, str], existing: dict[str, str]) -> str | None:
    if set(updates) != set(KEYS):
        return "Send SDK_VERSION, ROBOT_MODEL, SIMULATOR_CPU, and SIMULATOR_MEMORY."
    for key, value in updates.items():
        if not isinstance(value, str) or not value.strip():
            return f"{key} cannot be empty."
        cleaned = value.strip()
        updates[key] = cleaned
        if key == "ROBOT_MODEL" and cleaned not in MODELS and cleaned != existing.get(key):
            return f"{cleaned} is not a known robot model."
        if not VALIDATORS[key].fullmatch(cleaned) and cleaned != existing.get(key):
            return f"{key} has an unsupported value."
        if key == "SIMULATOR_CPU" and float(cleaned) > 128:
            return "SIMULATOR_CPU must be 128 or less."
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "EnvEditor/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}", flush=True)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_env(self) -> str:
        # newline="" keeps CRLF intact. The default translation would
        # rewrite the file as LF on the next save.
        return ENV_PATH.read_text(encoding="utf-8", newline="")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            if not HTML_PATH.is_file():
                self._send_json(500, {"error": "Editor page is missing."})
                return
            self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/env":
            self._handle_read()
            return
        self._send_json(404, {"error": "Not found."})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path not in ("/api/env", "/api/preview"):
            self._send_json(404, {"error": "Not found."})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0 or length > 16_384:
            self._send_json(400, {"error": "Request body is empty or too large."})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "Body must be JSON."})
            return
        values = payload.get("values")
        if not isinstance(values, dict):
            self._send_json(400, {"error": "Expected a values object."})
            return
        self._handle_update(values, write=path == "/api/env")

    def _handle_read(self) -> None:
        if not ENV_PATH.is_file():
            self._send_json(404, {"error": f"{ENV_PATH.name} was not found."})
            return
        text = self._read_env()
        self._send_json(200, {"values": {key: parse_env(text).get(key, "") for key in KEYS}, "raw": text})

    def _handle_update(self, values: dict, write: bool) -> None:
        if not ENV_PATH.is_file():
            self._send_json(404, {"error": f"{ENV_PATH.name} was not found."})
            return
        current = self._read_env()
        existing = parse_env(current)
        updates = {key: values.get(key, "") for key in KEYS}
        error = validate(updates, existing)
        if error:
            self._send_json(400, {"error": error})
            return
        revised = update_env(current, updates)
        if write:
            ENV_PATH.write_bytes(revised.encode("utf-8"))
        self._send_json(200, {"values": updates, "raw": revised, "saved": write})


def main() -> None:
    parser = argparse.ArgumentParser(description="Edit .env.example in a browser.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Environment editor: {url}", flush=True)
    print(f"Writing: {ENV_PATH}", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
