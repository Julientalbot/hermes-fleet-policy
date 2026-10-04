"""Tiny OpenAI-compatible stub for the end-to-end test (127.0.0.1 only, no network).

Answers ``POST /v1/chat/completions`` (JSON or SSE) with a fixed reply and echoes the request's
``service_tier`` back as the served tier. Each request body is appended to ``$STUB_LOG`` so the
test can compare what Hermes really sent with what the plugin recorded.

Run: ``python fake_provider.py <port-file> <log-file>``.
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    log_file = ""

    def log_message(self, *args):  # silence
        pass

    def _send_json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._send_json(200, {"object": "list", "data": [
                {"id": "stub-model", "object": "model", "context_length": 200000}]})
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        with open(self.log_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: body.get(k) for k in (
                "model", "reasoning_effort", "service_tier", "reasoning", "stream")}) + "\n")
        tier = body.get("service_tier") or "default"
        model = body.get("model") or "stub-model"
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        if not body.get("stream"):
            return self._send_json(200, {
                "id": "chatcmpl-stub", "object": "chat.completion", "created": int(time.time()),
                "model": model, "service_tier": tier,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "pong"}}],
                "usage": usage,
            })
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        base = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "created": int(time.time()),
                "model": model, "service_tier": tier}
        chunks = [
            {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "pong"},
                                  "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**base, "choices": [], "usage": usage},
        ]
        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    port_file, log_file = sys.argv[1], sys.argv[2]
    Handler.log_file = log_file
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with open(port_file, "w") as fh:
        fh.write(str(server.server_address[1]))
    server.serve_forever()


if __name__ == "__main__":
    main()
