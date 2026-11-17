"""HTTP integration tests; no real mihomo or systemd operations are performed."""
import base64
import contextlib
import http.client
import importlib.util
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("manager", Path(__file__).resolve().parents[1] / "manager.py")
manager = importlib.util.module_from_spec(spec)
with tempfile.TemporaryDirectory() as runtime:
    with patch.dict(os.environ, {"MIHOMO_DIR": runtime}):
        spec.loader.exec_module(manager)


class LoggingHandler(manager.Handler):
    def log_message(self, fmt, *args):
        with contextlib.redirect_stderr(self.server.access_log):
            super().log_message(fmt, *args)


class AuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = manager.ThreadingHTTPServer(("127.0.0.1", 0), LoggingHandler)
        cls.server.daemon_threads = True
        cls.server.access_log = io.StringIO()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.token = "test-token-not-a-real-credential"
        self.password = "test-password"
        self.settings = patch.multiple(manager.Handler, TOKEN=self.token, PASSWORD=self.password)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.server.access_log.seek(0)
        self.server.access_log.truncate()

    def request(self, method="GET", path="/", headers=None, body=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def login(self):
        status, headers, _ = self.request("POST", "/api/auth/login",
                                         {"Content-Type": "application/json"},
                                         json.dumps({"token": self.token}))
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_missing_credentials_require_authentication(self):
        status, headers, _ = self.request()
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])

    def test_basic_authentication_remains_compatible(self):
        credentials = base64.b64encode(f"{manager.AUTH_USER}:{self.password}".encode()).decode()
        status, _, body = self.request(headers={"Authorization": "Basic " + credentials})
        self.assertEqual(status, 200)
        self.assertIn(b"<title>mihomo", body)

    def test_invalid_basic_credentials_are_rejected(self):
        for value in ["Basic !!!", "Basic YWRtaW46d3Jvbmc=", "Basic "]:
            with self.subTest(value=value):
                self.assertEqual(self.request(headers={"Authorization": value})[0], 401)

    def test_bearer_authentication(self):
        status, headers, _ = self.request(headers={"Authorization": "Bearer " + self.token})
        self.assertEqual(status, 200)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.request(headers={"Authorization": "bearer " + self.token})[0], 200)
        for token in ["", "incorrect"]:
            self.assertEqual(self.request(headers={"Authorization": "Bearer " + token})[0], 401)

    def test_token_only_configuration_requires_credentials(self):
        with patch.object(manager.Handler, "PASSWORD", ""):
            self.assertEqual(self.request()[0], 401)
            empty_basic = base64.b64encode(f"{manager.AUTH_USER}:".encode()).decode()
            self.assertEqual(self.request(headers={"Authorization": "Basic " + empty_basic})[0], 401)
            self.assertEqual(self.request(headers={"Authorization": "Bearer " + self.token})[0], 200)

    def test_unconfigured_token_cannot_log_in(self):
        with patch.object(manager.Handler, "TOKEN", ""):
            self.assertEqual(self.request(path="/?token=" + self.token)[0], 401)
            self.assertEqual(self.request(headers={"Authorization": "Bearer " + self.token})[0], 401)

    def test_existing_no_auth_mode(self):
        with patch.multiple(manager.Handler, TOKEN="", PASSWORD=""):
            self.assertEqual(self.request()[0], 200)
            self.assertEqual(self.request(path="/?token=")[0], 401)

    def test_query_login_redirects_to_clean_url(self):
        status, headers, _ = self.request(path="/?token=" + self.token)
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/")
        cookie = SimpleCookie(headers["Set-Cookie"])[manager.SESSION_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(int(cookie["max-age"]), manager.SESSION_MAX_AGE)
        self.assertNotIn(self.token, cookie.value)
        self.assertEqual(self.request(headers={"Cookie": cookie.OutputString()})[0], 200)

    def test_invalid_and_ambiguous_query_tokens_are_rejected(self):
        for query in ["token=", "token=wrong", f"token={self.token}&token=",
                      f"token={self.token}&token={self.token}"]:
            with self.subTest(query=query):
                status, headers, _ = self.request(path="/?" + query)
                self.assertEqual(status, 401)
                self.assertNotIn("Set-Cookie", headers)

    def test_query_token_is_not_accepted_on_api_routes(self):
        self.assertEqual(self.request(path="/api/config?token=" + self.token)[0], 401)

    def test_fragment_login_page_is_public_and_clears_token(self):
        status, _, body = self.request(path="/login")
        self.assertEqual(status, 200)
        self.assertIn(b"location.hash", body)
        self.assertIn(b"history.replaceState", body)
        self.assertIn(b"/api/auth/login", body)
        self.assertNotIn(manager.HTML.encode(), body)

    def test_json_login_creates_session_for_get_and_post(self):
        cookie = self.login()
        self.assertEqual(self.request(headers={"Cookie": cookie})[0], 200)
        # Unknown routes return 404 only after authentication has succeeded.
        self.assertEqual(self.request("POST", "/api/unknown", {"Cookie": cookie}, "{}")[0], 404)

    def test_invalid_login_requests(self):
        for body, expected in [("not JSON", 400), ("[]", 400), ("null", 400),
                               ("{}", 401), ('{"token":1}', 401),
                               ('{"token":null}', 401), ('{"token":"wrong"}', 401)]:
            with self.subTest(body=body):
                status, headers, _ = self.request("POST", "/api/auth/login", body=body)
                self.assertEqual(status, expected)
                self.assertNotIn("Set-Cookie", headers)

    def test_forged_and_malformed_sessions_are_rejected(self):
        cookie = self.login()
        forged = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
        for value in [forged, manager.SESSION_COOKIE + "=abc", manager.SESSION_COOKIE + "=1.x.\xe9"]:
            self.assertEqual(self.request(headers={"Cookie": value})[0], 401)

    def test_invalid_login_body_sizes_are_rejected(self):
        for length in ["-1", "4097", "invalid"]:
            with self.subTest(length=length):
                status, _, _ = self.request("POST", "/api/auth/login", {"Content-Length": length})
                self.assertEqual(status, 400)

    def test_expired_sessions_are_rejected(self):
        expired_time = time.time() - manager.SESSION_MAX_AGE - 10
        with patch.object(manager.time, "time", return_value=expired_time):
            cookie = self.login()
        self.assertEqual(self.request(headers={"Cookie": cookie})[0], 401)

    def test_token_rotation_revokes_sessions(self):
        cookie = self.login()
        with patch.object(manager.Handler, "TOKEN", "a-new-token"):
            self.assertEqual(self.request(headers={"Cookie": cookie})[0], 401)
            self.assertEqual(self.request(headers={"Authorization": "Bearer " + self.token})[0], 401)
            self.assertEqual(self.request(headers={"Authorization": "Bearer a-new-token"})[0], 200)

    def test_session_is_valid_on_another_server_with_same_token(self):
        cookie = self.login()
        other = manager.ThreadingHTTPServer(("127.0.0.1", 0), LoggingHandler)
        other.access_log = io.StringIO()
        thread = threading.Thread(target=other.handle_request, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection(*other.server_address, timeout=5)
        try:
            conn.request("GET", "/", headers={"Cookie": cookie})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
        finally:
            conn.close()
            thread.join(timeout=5)
            other.server_close()

    def test_logout_clears_the_session_cookie(self):
        status, headers, _ = self.request("POST", "/api/auth/logout", {"Cookie": self.login()}, "{}")
        self.assertEqual(status, 200)
        self.assertEqual(SimpleCookie(headers["Set-Cookie"])[manager.SESSION_COOKIE]["max-age"], "0")

    def test_access_logs_redact_normal_and_encoded_query_tokens(self):
        for key in ["token", "%74oken"]:
            self.assertEqual(self.request(path=f"/?{key}={self.token}")[0], 303)
        logs = self.server.access_log.getvalue()
        self.assertNotIn(self.token, logs)
        self.assertIn("[REDACTED]", logs)

    def test_sensitive_responses_disable_caching_and_referrers(self):
        for path in ["/", "/login", "/?token=" + self.token]:
            _, headers, _ = self.request(path=path)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(headers["Referrer-Policy"], "no-referrer")


class TokenConfigurationTests(unittest.TestCase):
    def test_token_file_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manager.token"
            path.write_text("  file-token\n")
            with patch.object(manager, "TOKEN_FILE", path):
                with patch.dict(os.environ, {"MANAGER_TOKEN": "env-token"}):
                    self.assertEqual(manager.load_token(), "env-token")
                with patch.dict(os.environ, {"MANAGER_TOKEN": ""}):
                    self.assertEqual(manager.load_token(), "file-token")
                    path.unlink()
                    self.assertEqual(manager.load_token(), "")


if __name__ == "__main__":
    unittest.main()
