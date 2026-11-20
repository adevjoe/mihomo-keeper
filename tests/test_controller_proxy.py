"""Tests for the built-in dashboard's controller proxy (/api/mihomo/*, /api/stream/overview).

A local fake controller stands in for mihomo; no real mihomo or systemd is needed.
"""
import base64
import http.client
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("manager_proxy", Path(__file__).resolve().parents[1] / "manager.py")
manager = importlib.util.module_from_spec(spec)
with tempfile.TemporaryDirectory() as runtime:
    with patch.dict(os.environ, {"MIHOMO_DIR": runtime}):
        spec.loader.exec_module(manager)

SECRET = "controller-secret"
PASSWORD = "test-password"


class FakeController(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.requests.append({"method": self.command, "path": self.path, "body": body,
                                     "auth": self.headers.get("Authorization")})

    def _send(self, code, obj=None):
        body = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record()
        if self.path in ("/traffic", "/memory", "/logs?level=debug"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            line = {"/traffic": {"up": 1, "down": 2}, "/memory": {"inuse": 3, "oslimit": 0}}.get(
                self.path, {"type": "info", "payload": "hello"})
            for _ in range(2):
                self.wfile.write(json.dumps(line).encode() + b"\n")
                self.wfile.flush()
            return
        if self.path == "/connections":
            self._send(200, {"downloadTotal": 10, "uploadTotal": 5, "connections": [{"id": "a"}, {"id": "b"}]})
        elif self.path.startswith("/proxies/dead/delay"):
            self._send(503, {"message": "An error occurred in the delay test"})
        else:
            self._send(200, {"ok": True, "path": self.path})

    def do_PUT(self):
        self._record()
        self._send(204)

    do_PATCH = do_PUT
    do_DELETE = do_PUT
    do_POST = do_PUT


class ControllerProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctrl = ThreadingHTTPServer(("127.0.0.1", 0), FakeController)
        cls.ctrl.daemon_threads = True
        cls.ctrl.requests = []
        threading.Thread(target=cls.ctrl.serve_forever, daemon=True).start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), manager.Handler)
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.config = Path(cls.tmp.name) / "config.yaml"
        cls.config.write_text(f"external-controller: 127.0.0.1:{cls.ctrl.server_address[1]}\nsecret: {SECRET}\n")

    @classmethod
    def tearDownClass(cls):
        for s in (cls.server, cls.ctrl):
            s.shutdown()
            s.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        patches = [patch.object(manager, "CONFIG_FILE", self.config),
                   patch.multiple(manager.Handler, PASSWORD=PASSWORD, TOKEN="")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        manager._META_CACHE.update(key=None)
        # 上一个用例的实时流结束后，后台线程可能还会打出最后一个上游请求，等它们停下再清空
        n = -1
        while n != len(self.ctrl.requests):
            n = len(self.ctrl.requests)
            time.sleep(0.2)
        self.ctrl.requests.clear()

    def request(self, method, path, body=None, auth=True, headers=None):
        h = dict(headers or {})
        if auth:
            h["Authorization"] = "Basic " + base64.b64encode(f"{manager.AUTH_USER}:{PASSWORD}".encode()).decode()
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        try:
            conn.request(method, path, body=body, headers=h)
            r = conn.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            conn.close()

    def test_requires_manager_authentication(self):
        for method in ("GET", "PUT", "PATCH", "DELETE", "POST"):
            with self.subTest(method=method):
                self.assertEqual(self.request(method, "/api/mihomo/version", auth=False)[0], 401)
        self.assertEqual(self.request("GET", "/api/stream/overview", auth=False)[0], 401)
        self.assertEqual(self.ctrl.requests, [])

    def test_get_adds_secret_and_keeps_encoded_path_and_query(self):
        path = "/proxies/%E8%8A%82%E7%82%B9%20a%2Fb/delay?url=https%3A%2F%2Fx&timeout=3000"
        status, _, body = self.request("GET", "/api/mihomo" + path)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["path"], path)
        self.assertEqual(self.ctrl.requests[-1]["auth"], f"Bearer {SECRET}")

    def test_write_methods_forward_body_and_relay_no_content(self):
        for method in ("PUT", "PATCH", "DELETE", "POST"):
            with self.subTest(method=method):
                status, _, body = self.request(method, "/api/mihomo/configs", b'{"mode":"global"}',
                                               headers={"Content-Type": "application/json"})
                self.assertEqual((status, body), (204, b""))
                req = self.ctrl.requests[-1]
                self.assertEqual((req["method"], req["path"], req["body"]), (method, "/configs", b'{"mode":"global"}'))

    def test_upstream_errors_are_relayed(self):
        status, _, body = self.request("GET", "/api/mihomo/proxies/dead/delay?timeout=1")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["message"], "An error occurred in the delay test")

    def test_disallowed_paths_are_rejected(self):
        for path in ["/api/mihomo/restart", "/api/mihomo/upgrade", "/api/mihomo/", "/api/mihomo/debug/pprof",
                     "/api/mihomo/proxies/../restart", "/api/mihomo/proxies/%2e%2E/restart",
                     "/api/mihomo/configsx"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, b"{}")[0], 403)
        self.assertEqual(self.ctrl.requests, [])

    def test_oversized_body_is_rejected(self):
        status, _, _ = self.request("PUT", "/api/mihomo/configs", headers={"Content-Length": str(manager.CTRL_MAX_BODY + 1)})
        self.assertEqual(status, 400)

    def test_streams_are_relayed_line_by_line(self):
        status, headers, body = self.request("GET", "/api/mihomo/logs?level=debug")
        self.assertEqual(status, 200)
        self.assertIn("ndjson", headers["Content-Type"])
        self.assertEqual([json.loads(l) for l in body.splitlines()], [{"type": "info", "payload": "hello"}] * 2)

    def test_overview_stream_merges_topics(self):
        status, _, body = self.request("GET", "/api/stream/overview")
        self.assertEqual(status, 200)
        msgs = [json.loads(l) for l in body.splitlines()]
        topics = {k for m in msgs for k in m}
        self.assertTrue({"traffic", "memory"} & topics, msgs)
        for m in msgs:
            if "traffic" in m:
                self.assertEqual(m["traffic"], {"up": 1, "down": 2})
            if "conns" in m:
                self.assertEqual(m["conns"], {"count": 2, "uploadTotal": 5, "downloadTotal": 10})

    def test_status_never_exposes_controller_secret(self):
        status, _, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertNotIn("dashboard", json.loads(body))
        self.assertNotIn(SECRET.encode(), body)
        status, _, page = self.request("GET", "/")
        self.assertNotIn(b'id="dash"', page)

    def test_unreachable_controller_returns_502(self):
        dead = Path(self.tmp.name) / "dead.yaml"
        with socket_free_port() as port:
            dead.write_text(f"external-controller: 127.0.0.1:{port}\n")
        with patch.object(manager, "CONFIG_FILE", dead):
            manager._META_CACHE.update(key=None)
            status, _, body = self.request("GET", "/api/mihomo/version")
        self.assertEqual(status, 502)
        self.assertIn("controller unreachable", json.loads(body)["message"])


class socket_free_port:
    """Reserve and release a local port, so nothing is listening on it afterwards."""

    def __enter__(self):
        import socket
        self.s = socket.socket()
        self.s.bind(("127.0.0.1", 0))
        return self.s.getsockname()[1]

    def __exit__(self, *exc):
        self.s.close()


if __name__ == "__main__":
    unittest.main()
