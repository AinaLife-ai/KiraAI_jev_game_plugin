"""Local mock TypeSafe /v1/systemone server for offline end-to-end testing.

Implements the real TypeSafe API contract so JevClient (our plugin's backend)
can hit it and get real structured answers — without a valid API key and
without network access to api.typesafe.ai.

Rule: NO real Jev inference — this is a deterministic canned responder that
respects the request schema (state + questions) and returns typed answers with
calibrated-looking probabilities so the whole pipeline can be exercised.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class MockJevHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):  # silence access logs
        pass

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization", "").startswith("Bearer ")

    def do_GET(self):
        if self.path == "/v1/models":
            if not self._auth_ok():
                self._send(401, {"detail": {"error_type": "authentication_error"}})
                return
            self._send(200, {"models": [{"name": "jev-latest", "description": "mock"}]})
            return
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        if self.path != "/v1/systemone":
            self._send(404, {"detail": "not found"})
            return
        if not self._auth_ok():
            self._send(401, {"detail": {"error_type": "authentication_error"}})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self._send(400, {"detail": "bad json"})
            return
        state = req.get("state")
        questions = req.get("questions", {})
        answers = {}
        for qid, q in questions.items():
            qtype = q.get("type")
            if qtype == "noul":
                answers[qid] = {"type": "noul", "noul": 0.9, "confidence": 0.95}
            elif qtype == "choice":
                crit = q.get("criteria", {})
                keys = list(crit) if isinstance(crit, dict) else list(crit or [])
                # Rotate through options instead of always picking the first,
                # so repeated calls don't loop on the same action.
                self._choice_counter = getattr(self, "_choice_counter", 0) + 1
                idx = (self._choice_counter - 1) % max(1, len(keys))
                pick = keys[idx] if keys else ""
                probs = {k: (0.8 if k == pick else 0.2 / max(1, len(keys) - 1)) for k in keys}
                answers[qid] = {"type": "choice", "choice": pick,
                                "probabilities": probs, "confidence": 0.8}
            elif qtype == "score":
                crit = q.get("criteria", [])
                answers[qid] = {"type": "score", "score": 1.6,
                                "legend": {str(i): c for i, c in enumerate(crit)},
                                "probabilities": {str(i): round(1.0 / max(1, len(crit)), 3) for i in range(len(crit))},
                                "confidence": 0.85}
        # Approximate token usage: rough char/4 heuristic
        n_in = max(1, len(json.dumps({"state": state, "questions": questions})) // 4)
        self._send(200, {
            "model": req.get("model", "jev-latest"),
            "answers": answers,
            "usage": {"input_tokens": n_in, "output_tokens": 12},
        })

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main(port: int = 17889):
    srv = HTTPServer(("127.0.0.1", port), MockJevHandler)
    print(f"mock Jev listening on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()