#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="cr_agent mock callback server")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=9999, help="监听端口")
    parser.add_argument(
        "--output",
        default="runtime/mock_callback_requests.jsonl",
        help="回调落盘文件，默认写入项目 runtime 目录",
    )
    return parser.parse_args()


class CallbackHandler(BaseHTTPRequestHandler):
    output_path: Path

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        record = {
            "received_at": datetime.now().isoformat(),
            "path": self.path,
            "headers": dict(self.headers),
            "body": parse_body(body),
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

        print("\n=== CALLBACK RECEIVED ===")
        print(f"path: {self.path}")
        print(json.dumps(record["body"], ensure_ascii=False, indent=2))

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


def parse_body(body: str):
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def main() -> None:
    args = parse_args()
    CallbackHandler.output_path = Path(args.output).resolve()
    server = HTTPServer((args.host, args.port), CallbackHandler)
    print(f"mock callback server listening on http://{args.host}:{args.port}")
    print(f"output file: {CallbackHandler.output_path}")
    server.serve_forever()


if __name__ == "__main__":
    main()
