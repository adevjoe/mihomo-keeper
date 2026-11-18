"""Tests for how update-subscription.sh applies a new config.

No real mihomo, systemd or network operations are performed: `systemctl` and `mihomo`
are replaced by stubs on PATH, the subscription is served from a file:// URL, and the
mihomo external controller is a local HTTP server that records the requests it gets.
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "update-subscription.sh"
MERGE_SCRIPT = REPO_ROOT / "merge-config.py"

SECRET = "test-secret-123"

SYSTEMCTL_STUB = """#!/usr/bin/env bash
# Records every invocation and keeps a tiny active/inactive state machine:
# the service starts out inactive when STUB_SERVICE_INACTIVE is set, and a
# restart activates it.
printf '%s\\n' "$*" >> "$STUB_LOG"
state="$STUB_STATE"
if [ ! -f "$state" ]; then
  if [ -n "${STUB_SERVICE_INACTIVE:-}" ]; then
    echo inactive > "$state"
  else
    echo active > "$state"
  fi
fi
case "$1" in
  is-active) [ "$(cat "$state")" = active ] ;;
  restart|start) echo active > "$state" ;;
  stop) echo inactive > "$state" ;;
  status) : ;;
esac
"""

MIHOMO_STUB = """#!/usr/bin/env bash
# `mihomo -t ...` only validates, and the stub always accepts.
exit 0
"""

SUBSCRIPTION = """mixed-port: 7890
mode: rule
external-controller: 127.0.0.1:{port}
secret: {secret}
proxies: []
proxy-groups: []
rules:
- MATCH,DIRECT
"""


class Controller(http.server.BaseHTTPRequestHandler):
    """Stands in for the mihomo external controller."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _record(self, method):
        self.server.requests.append(
            {
                "method": method,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )

    def do_PUT(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self._record("PUT")
        self.server.requests[-1]["body"] = body.decode()
        self.send_response(self.server.put_status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._record("GET")
        payload = json.dumps({"version": "stub", "meta": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ApplyModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.bin_dir = self.tmp / "bin"
        self.mihomo_dir = self.tmp / "mihomo"
        self.bin_dir.mkdir()
        self.mihomo_dir.mkdir()

        self.log = self.tmp / "systemctl.log"
        self.state = self.tmp / "service.state"

        for name, body in (("systemctl", SYSTEMCTL_STUB), ("mihomo", MIHOMO_STUB)):
            path = self.bin_dir / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

        self.port = free_port()
        self.subscription = self.tmp / "sub.yaml"
        self.subscription.write_text(
            SUBSCRIPTION.format(port=self.port, secret=SECRET), encoding="utf-8"
        )

        self.server = None

    def start_controller(self, put_status=204):
        server = http.server.HTTPServer(("127.0.0.1", self.port), Controller)
        server.requests = []
        server.put_status = put_status
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join()

        self.addCleanup(stop)
        self.server = server
        return server

    def run_script(self, **extra_env):
        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{self.bin_dir}{os.pathsep}{env['PATH']}",
                "MIHOMO_DIR": str(self.mihomo_dir),
                "MIHOMO_BIN": str(self.bin_dir / "mihomo"),
                "MIHOMO_SERVICE": "stub-mihomo",
                "MIHOMO_SUB_URL": self.subscription.as_uri(),
                "MERGE_SCRIPT": str(MERGE_SCRIPT),
                "STUB_LOG": str(self.log),
                "STUB_STATE": str(self.state),
                "NO_COLOR": "1",
            }
        )
        env.update(extra_env)
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def systemctl_calls(self, verb):
        if not self.log.exists():
            return []
        return [
            line
            for line in self.log.read_text(encoding="utf-8").splitlines()
            if line.startswith(verb)
        ]

    def put_requests(self):
        return [r for r in self.server.requests if r["method"] == "PUT"]

    def test_default_mode_hot_reloads_without_restarting(self):
        self.start_controller()
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new config applied by: reload", result.stdout)
        self.assertEqual(self.systemctl_calls("restart"), [])

        puts = self.put_requests()
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["path"], "/configs?force=false")
        self.assertEqual(puts[0]["authorization"], f"Bearer {SECRET}")
        self.assertEqual(
            json.loads(puts[0]["body"]),
            {"path": str(self.mihomo_dir / "config.yaml")},
        )

    def test_restart_mode_never_calls_the_controller_configs_endpoint(self):
        self.start_controller()
        result = self.run_script(MIHOMO_APPLY_MODE="restart")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new config applied by: restart", result.stdout)
        self.assertEqual(len(self.systemctl_calls("restart")), 1)
        self.assertEqual(self.put_requests(), [])

    def test_reload_falls_back_to_restart_when_controller_rejects_it(self):
        self.start_controller(put_status=500)
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("falling back to a restart", result.stdout)
        self.assertIn("new config applied by: restart", result.stdout)
        self.assertEqual(len(self.put_requests()), 1)
        self.assertEqual(len(self.systemctl_calls("restart")), 1)

    def test_reload_is_skipped_when_the_service_is_not_running(self):
        self.start_controller()
        result = self.run_script(STUB_SERVICE_INACTIVE="1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new config applied by: restart", result.stdout)
        self.assertEqual(self.put_requests(), [])
        self.assertEqual(len(self.systemctl_calls("restart")), 1)

    def test_missing_secret_still_reloads(self):
        self.subscription.write_text(
            SUBSCRIPTION.format(port=self.port, secret="").replace("secret: \n", ""),
            encoding="utf-8",
        )
        self.start_controller()
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new config applied by: reload", result.stdout)
        puts = self.put_requests()
        self.assertEqual(len(puts), 1)
        self.assertIsNone(puts[0]["authorization"])

    def test_unchanged_config_applies_nothing(self):
        self.start_controller()
        self.assertEqual(self.run_script().returncode, 0)

        self.server.requests.clear()
        self.log.unlink(missing_ok=True)
        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("config unchanged, nothing to apply", result.stdout)
        self.assertEqual(self.put_requests(), [])
        self.assertEqual(self.systemctl_calls("restart"), [])

    def test_invalid_apply_mode_aborts_before_writing_anything(self):
        result = self.run_script(MIHOMO_APPLY_MODE="bogus")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MIHOMO_APPLY_MODE", result.stderr)
        self.assertFalse((self.mihomo_dir / "config.yaml").exists())
        self.assertEqual(self.systemctl_calls("restart"), [])


if __name__ == "__main__":
    unittest.main()
