#!/usr/bin/env python3
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def tool_message(call_id, name, arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, separators=(",", ":"))},
        }],
    }


def chunk(delta, finish_reason=None):
    return {
        "id": "chatcmpl-kungfu-fixture",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "fixture-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = b'{"ok":true}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        stage = sum(message.get("role") == "tool" for message in request.get("messages", []))
        mode = os.environ.get("MOCK_MODE", "positive")
        if stage == 0:
            message = tool_message("call-read", "read", {"filePath": "/workspace/input.txt"})
        elif stage == 1:
            message = tool_message("call-write", "write", {
                "filePath": "/workspace/output.txt",
                "content": "KUNGFU_OPENCODE_CI_OK\n",
            })
        elif stage == 2:
            command = "false" if mode == "false-success" else (
                "printf 'bash-ok\\n' > /workspace/bash-ok.txt "
                "&& test \"$(cat /workspace/output.txt)\" = KUNGFU_OPENCODE_CI_OK"
            )
            message = tool_message("call-bash", "bash", {
                "command": command,
                "description": "Verify the deterministic fixture",
            })
        else:
            message = {"role": "assistant", "content": "Fixture complete."}

        finish = "tool_calls" if message.get("tool_calls") else "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        if message.get("tool_calls"):
            call = message["tool_calls"][0]
            delta = {
                "role": "assistant",
                "tool_calls": [{
                    "index": 0, "id": call["id"], "type": "function",
                    "function": call["function"],
                }],
            }
        else:
            delta = {"role": "assistant", "content": message["content"]}
        self.write_event(chunk(delta))
        self.write_event(chunk({}, finish))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def write_event(self, payload):
        self.wfile.write(b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
