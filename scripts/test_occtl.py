#!/usr/bin/env python3
"""occtl.py 的测试:用一个 stdlib 假 opencode 服务器(含 SSE)跑 CLI 子进程。

运行:python3.11 -m unittest discover -s . -p 'test_*.py'
"""

from __future__ import annotations

import base64
import json
import queue
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLI = Path(__file__).parent / "occtl.py"


class FakeOpencode:
    """覆盖 occtl 用到的端点:/api/info、会话 CRUD、prompt、interrupt、wait、SSE。"""

    def __init__(self, password: str = ""):
        self.password = password
        self.sessions: dict[str, dict] = {}
        self.active: dict[str, dict] = {}
        self.prompts: list[dict] = []
        self.messages: dict[str, list] = {}
        self.subs: list[queue.Queue] = []
        self.interrupts = 0
        self.lock = threading.Lock()
        self.on_prompt = None
        # 版本形状开关(默认 stable/v2.0.x 行为)
        self.prompt_shape = "flat"       # "flat" | "nested"
        self.wait_path = "experimental"  # "experimental" | "stable"
        self.info_path = "info"          # "info" | "server"
        self.reject_permissions = False  # True = create 拒绝 permissions 字段(next 版)
        self.break_stream_after: int | None = None  # SSE 写 N 条事件后断开
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静音
                pass

            # ---------- 基础设施 ----------
            def _auth_ok(self) -> bool:
                if not outer.password:
                    return True
                header = self.headers.get("Authorization", "")
                if not header.startswith("Basic "):
                    return False
                try:
                    _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                except Exception:
                    return False
                return pw == outer.password

            def _send(self, code: int, payload=None):
                body = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(code)
                if body:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    return json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    return {}

            def _unauthorized(self):
                self._send(401, {"_tag": "UnauthorizedError", "message": "Authentication required"})

            def _path(self) -> str:
                return urllib.parse.urlparse(self.path).path

            # ---------- GET ----------
            def do_GET(self):
                if not self._auth_ok():
                    return self._unauthorized()
                path = self._path()
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if path == "/api/info":
                    if outer.info_path == "server":
                        return self._send(404, {"_tag": "NotFoundError", "message": "no such route"})
                    return self._send(200, {"version": "fake-1", "pid": 1, "urls": []})
                if path == "/api/server":
                    if outer.info_path == "info":
                        return self._send(404, {"_tag": "NotFoundError", "message": "no such route"})
                    return self._send(200, {"urls": [outer.url]})
                if path == "/api/session":
                    with outer.lock:
                        data = list(outer.sessions.values())
                    directory = query.get("directory", [None])[0]
                    if directory:
                        data = [s for s in data if (s.get("location") or {}).get("directory") == directory]
                    return self._send(200, {"data": data})
                if path == "/api/session/active":
                    with outer.lock:
                        return self._send(200, {"data": dict(outer.active)})
                if path == "/api/event":
                    return self._sse()
                m = re.fullmatch(r"/api/session/([^/]+)", path)
                if m:
                    with outer.lock:
                        sess = outer.sessions.get(m.group(1))
                    if not sess:
                        return self._send(404, {"_tag": "SessionNotFoundError", "message": "not found"})
                    return self._send(200, {"data": sess})
                m = re.fullmatch(r"/api/session/([^/]+)/message", path)
                if m:
                    with outer.lock:
                        data = outer.messages.get(m.group(1))
                    return self._send(200, {"data": data if data is not None else [], "cursor": {}})
                return self._send(404, {"_tag": "NotFoundError", "message": path})

            # ---------- POST ----------
            def do_POST(self):
                if not self._auth_ok():
                    return self._unauthorized()
                path = self._path()
                if path == "/api/session":
                    body = self._read_json()
                    if outer.reject_permissions and "permissions" in body:
                        return self._send(400, {"_tag": "InvalidRequestError", "message": "unknown field: permissions"})
                    with outer.lock:
                        sid = f"ses_test_{len(outer.sessions) + 1}"
                        sess = {
                            "id": sid,
                            "title": body.get("title", ""),
                            "location": body.get("location") or {"directory": ""},
                            "time": {"created": 1, "updated": 1},
                        }
                        if body.get("permissions"):
                            sess["permissions"] = body["permissions"]
                        outer.sessions[sid] = sess
                    return self._send(200, {"data": sess})
                m = re.fullmatch(r"/api/session/([^/]+)/prompt", path)
                if m:
                    sid = m.group(1)
                    body = self._read_json()
                    if outer.prompt_shape == "nested":
                        if "prompt" not in body:
                            return self._send(400, {"_tag": "InvalidRequestError", "message": "prompt is required"})
                    elif "text" not in body:
                        return self._send(400, {"_tag": "InvalidRequestError", "message": "text is required"})
                    prompt_text = (body.get("prompt") or {}).get("text", "") if "prompt" in body else body.get("text", "")
                    with outer.lock:
                        exists = sid in outer.sessions
                        if exists:
                            outer.prompts.append({"sessionID": sid, "body": body})
                    if not exists:
                        return self._send(404, {"_tag": "SessionNotFoundError", "message": "not found"})
                    self._send(200, {"data": {"id": "msg_1", "sessionID": sid, "type": "user", "delivery": "steer"}})
                    cb = outer.on_prompt
                    if cb:
                        threading.Thread(target=cb, args=(sid, prompt_text), daemon=True).start()
                    return
                m = re.fullmatch(r"/api/session/([^/]+)/interrupt", path)
                if m:
                    with outer.lock:
                        outer.interrupts += 1
                    outer.emit("session.execution.interrupted", m.group(1), {"reason": "user"})
                    return self._send(200, {"interrupted": True})
                m = re.fullmatch(r"/api/experimental/session/([^/]+)/wait", path)
                if m:
                    if outer.wait_path == "stable":
                        return self._send(404, {"_tag": "NotFoundError", "message": "no such route"})
                    return self._send(204)
                m = re.fullmatch(r"/api/session/([^/]+)/wait", path)
                if m:
                    if outer.wait_path == "experimental":
                        return self._send(404, {"_tag": "NotFoundError", "message": "no such route"})
                    return self._send(204)
                return self._send(404, {"_tag": "NotFoundError", "message": path})

            # ---------- DELETE ----------
            def do_DELETE(self):
                if not self._auth_ok():
                    return self._unauthorized()
                m = re.fullmatch(r"/api/session/([^/]+)", self._path())
                if m:
                    with outer.lock:
                        outer.sessions.pop(m.group(1), None)
                    return self._send(204)
                return self._send(404, {"_tag": "NotFoundError", "message": self._path()})

            # ---------- SSE ----------
            def _sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q: queue.Queue = queue.Queue()
                with outer.lock:
                    outer.subs.append(q)
                try:
                    self._write_event({"id": "evt_0", "type": "server.connected", "data": {}})
                    if outer.break_stream_after == 0:
                        return
                    written = 0
                    while True:
                        try:
                            ev = q.get(timeout=30)
                        except queue.Empty:
                            break
                        self._write_event(ev)
                        written += 1
                        if outer.break_stream_after is not None and written >= outer.break_stream_after:
                            return  # 模拟事件流被掐断(不发送后续终态事件)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with outer.lock:
                        if q in outer.subs:
                            outer.subs.remove(q)

            def _write_event(self, ev: dict):
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def prompt_count(self) -> int:
        with self.lock:
            return len(self.prompts)

    # 事件注入
    def emit(self, ev_type: str, session_id: str, extra: dict | None = None):
        ev = {"id": "evt_x", "created": 0, "type": ev_type, "data": {"sessionID": session_id, **(extra or {})}}
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            q.put(ev)


def run_cli(*argv: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class OcctlTest(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOpencode(password="pw")
        self.addCleanup(self.srv.close)

    def cli(self, *argv: str):
        return run_cli("--server", self.srv.url, *argv)

    # ---------- 基础 ----------
    def test_info_auth(self):
        bad = self.cli("--password", "wrong", "info")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("401", bad.stderr)

        ok = self.cli("--password", "pw", "--json", "info")
        self.assertEqual(ok.returncode, 0)
        self.assertEqual(json.loads(ok.stdout)["version"], "fake-1")

    def test_session_crud(self):
        r = self.cli("--password", "pw", "--json", "new", "--dir", "D:/work/a")
        self.assertEqual(r.returncode, 0)
        sid = json.loads(r.stdout)["id"]

        r = self.cli("--password", "pw", "--json", "ls", "--dir", "D:/work/a")
        self.assertEqual([s["id"] for s in json.loads(r.stdout)], [sid])

        r = self.cli("--password", "pw", "--json", "ls", "--dir", "D:/work/other")
        self.assertEqual(json.loads(r.stdout), [])

        r = self.cli("--password", "pw", "rm", sid)
        self.assertEqual(r.returncode, 0)
        r = self.cli("--password", "pw", "--json", "ls")
        self.assertEqual(json.loads(r.stdout), [])

    # ---------- 跑任务 ----------
    def test_run_done_streams_and_succeeds(self):
        def on_prompt(sid, text):
            self.srv.emit("session.execution.started", sid)
            self.srv.emit("session.text.delta", sid, {"delta": "hello "})
            self.srv.emit("session.text.delta", sid, {"delta": "world"})
            self.srv.emit("session.execution.succeeded", sid)

        self.srv.on_prompt = on_prompt
        r = self.cli("--password", "pw", "--json", "run", "--dir", "D:/w", "--timeout", "10", "hi")
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "succeeded")
        self.assertIn("hello world", out["text"])
        self.assertTrue(out["sessionID"].startswith("ses_"))

    def test_run_failed_exit_2(self):
        def on_prompt(sid, text):
            self.srv.emit("session.execution.started", sid)
            self.srv.emit("session.execution.failed", sid, {"error": {"type": "ProviderError", "message": "boom"}})

        self.srv.on_prompt = on_prompt
        r = self.cli("--password", "pw", "--json", "run", "--dir", "D:/w", "--timeout", "10", "hi")
        self.assertEqual(r.returncode, 2)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "failed")
        self.assertIn("boom", out["error"])

    def test_run_timeout_interrupts(self):
        def on_prompt(sid, text):
            self.srv.emit("session.execution.started", sid)  # 永不结束

        self.srv.on_prompt = on_prompt
        r = self.cli("--password", "pw", "--json", "run", "--dir", "D:/w", "--timeout", "0.6", "hi")
        self.assertEqual(r.returncode, 3)
        self.assertGreaterEqual(self.srv.interrupts, 1)

    def test_run_resume_session(self):
        def on_prompt(sid, text):
            self.srv.emit("session.text.delta", sid, {"delta": text})
            self.srv.emit("session.execution.succeeded", sid)

        self.srv.on_prompt = on_prompt
        with self.srv.lock:
            self.srv.sessions["ses_existing"] = {
                "id": "ses_existing",
                "location": {"directory": "D:/w"},
                "time": {"created": 1, "updated": 1},
            }
        r = self.cli("--password", "pw", "--json", "run", "--session", "ses_existing", "--timeout", "10", "继续")
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertEqual(out["sessionID"], "ses_existing")
        self.assertEqual(out["text"], "继续")

    # ---------- 其他 ----------
    def test_events_max(self):
        r = self.cli("--password", "pw", "--json", "events", "--max", "1", "--timeout", "5")
        self.assertEqual(r.returncode, 0)
        first = json.loads(r.stdout.splitlines()[0])
        self.assertEqual(first["type"], "server.connected")

    def test_event_session_filter(self):
        def on_prompt(sid, text):
            self.srv.emit("session.text.delta", "ses_other", {"delta": "IGNORED"})
            self.srv.emit("session.execution.succeeded", "ses_other")
            self.srv.emit("session.text.delta", sid, {"delta": "kept"})
            self.srv.emit("session.execution.succeeded", sid)

        self.srv.on_prompt = on_prompt
        r = self.cli("--password", "pw", "--json", "run", "--dir", "D:/w", "--timeout", "10", "hi")
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)
        self.assertEqual(out["text"], "kept")

    def test_registry_name_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "servers.json"
            path.write_text(
                json.dumps({"servers": {"pc-01": {"url": self.srv.url, "password": "pw"}}}),
                encoding="utf-8",
            )
            r = run_cli("--servers", str(path), "--server", "pc-01", "--json", "info")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["version"], "fake-1")

            r = run_cli("--servers", str(path), "--server", "pc-404", "info")
            self.assertEqual(r.returncode, 1)
            self.assertIn("没有名为", r.stderr)

    def test_ps_fleet_view(self):
        with self.srv.lock:
            self.srv.sessions["ses_a"] = {
                "id": "ses_a",
                "location": {"directory": "D:/w"},
                "time": {"created": 1, "updated": 1},
            }
            self.srv.active["ses_a"] = {"type": "running"}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "servers.json"
            path.write_text(
                json.dumps(
                    {
                        "servers": {
                            "pc-on": {"url": self.srv.url, "password": "pw"},
                            "pc-off": {"url": "http://127.0.0.1:9", "password": "x"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            r = run_cli("--servers", str(path), "--json", "ps")
            self.assertEqual(r.returncode, 0, r.stderr)
            rows = {row["name"]: row for row in json.loads(r.stdout)}
            self.assertTrue(rows["pc-on"]["online"])
            self.assertIn("ses_a", rows["pc-on"]["active"])
            self.assertFalse(rows["pc-off"]["online"])

    def test_run_detach(self):
        r = self.cli("--password", "pw", "--json", "run", "--detach", "--dir", "D:/w", "hi")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "dispatched")
        self.assertTrue(out["sessionID"].startswith("ses_"))
        self.assertEqual(self.srv.prompt_count(), 1)

    # ---------- 版本兼容(stable v2.0.x ↔ dev/下一版) ----------
    def test_prompt_nested_fallback(self):
        self.srv.prompt_shape = "nested"

        def on_prompt(sid, text):
            self.srv.emit("session.text.delta", sid, {"delta": "nested ok"})
            self.srv.emit("session.execution.succeeded", sid)

        self.srv.on_prompt = on_prompt
        r = self.cli("--password", "pw", "--json", "run", "--dir", "D:/w", "--timeout", "10", "hi")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "succeeded")
        self.assertEqual(out["text"], "nested ok")
        with self.srv.lock:
            body = self.srv.prompts[0]["body"]
        self.assertIn("prompt", body)
        self.assertNotIn("text", body)

    def test_wait_stable_fallback(self):
        self.srv.wait_path = "stable"
        r = self.cli("--password", "pw", "--json", "wait", "ses_whatever")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "idle")

    def test_info_server_fallback(self):
        self.srv.info_path = "server"
        r = self.cli("--password", "pw", "--json", "info")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("urls", json.loads(r.stdout))

    def test_create_permissions_rejected_warns(self):
        self.srv.reject_permissions = True
        r = self.cli("--password", "pw", "--json", "new", "--dir", "D:/w")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("permissions", r.stderr)
        self.assertTrue(json.loads(r.stdout)["id"])

    def test_require_idle_refuses_when_busy(self):
        with self.srv.lock:
            self.srv.sessions["ses_busy"] = {
                "id": "ses_busy",
                "location": {"directory": "D:/other"},
                "time": {"created": 1, "updated": 1},
            }
            self.srv.active["ses_busy"] = {"type": "running"}
        r = self.cli("--password", "pw", "--json", "run", "--detach", "--require-idle", "--dir", "D:/w", "hi")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ses_busy", r.stderr)

        with self.srv.lock:
            self.srv.active.clear()
        r2 = self.cli("--password", "pw", "--json", "run", "--detach", "--require-idle", "--dir", "D:/w", "hi")
        self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_stream_break_recovers_state(self):
        self.srv.break_stream_after = 1

        def on_prompt(sid, text):
            with self.srv.lock:
                self.srv.sessions[sid]["outcome"] = "succeeded"
                self.srv.messages[sid] = [
                    {"id": "m1", "type": "assistant", "content": [{"type": "text", "text": "recovered text"}]}
                ]
            self.srv.emit("session.text.delta", sid, {"delta": "partial "})

        self.srv.on_prompt = on_prompt
        r = self.cli("--password", "pw", "--json", "run", "--dir", "D:/w", "--timeout", "10", "hi")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "succeeded")
        self.assertIn("partial", out["text"])

    def test_missing_server_flag(self):
        r = run_cli("info")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--server", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
