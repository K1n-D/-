import http.client
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("IOT_DB_ENABLED", "0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class AuthTests(unittest.TestCase):
    """Authentication is ON for this suite: tokens, 401s and throttling."""

    @classmethod
    def setUpClass(cls):
        cls.original_enabled = server.AUTH_ENABLED
        server.AUTH_ENABLED = True
        from http.server import ThreadingHTTPServer
        cls.http_server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.http_server.server_address[1]
        threading.Thread(target=cls.http_server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.http_server.shutdown()
        server.AUTH_ENABLED = cls.original_enabled

    def setUp(self):
        server.ACTIVE_TOKENS.clear()
        server.LOGIN_FAILURES.clear()

    def tearDown(self):
        server.AUTH_ENABLED = True

    def request(self, method, path, body=None, token=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, (json.loads(raw) if raw else None)

    def login(self):
        status, body = self.request("POST", "/api/auth/login",
                                    {"username": "admin", "password": "admin123"})
        self.assertEqual(status, 200)
        return body["token"]

    def test_health_stays_public(self):
        status, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)

    def test_data_api_rejects_missing_token(self):
        status, body = self.request("GET", "/api/devices")
        self.assertEqual(status, 401)

    def test_data_api_rejects_forged_token(self):
        status, _ = self.request("GET", "/api/devices", token="forged-token")
        self.assertEqual(status, 401)

    def test_login_issues_working_token(self):
        token = self.login()
        self.assertNotEqual(token, "local-demo-token")  # tokens are per-session random
        status, body = self.request("GET", "/api/devices", token=token)
        self.assertEqual(status, 200)

    def test_wrong_password_returns_401(self):
        status, _ = self.request("POST", "/api/auth/login",
                                 {"username": "admin", "password": "nope"})
        self.assertEqual(status, 401)

    def test_logout_revokes_token(self):
        token = self.login()
        status, _ = self.request("POST", "/api/auth/logout", {}, token=token)
        self.assertEqual(status, 200)
        status, _ = self.request("GET", "/api/devices", token=token)
        self.assertEqual(status, 401)

    def test_sse_requires_token(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/stream")
        response = conn.getresponse()
        self.assertEqual(response.status, 401)
        conn.close()

    def test_sse_accepts_query_token(self):
        token = self.login()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/api/stream?token={token}")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        conn.close()

    def test_repeated_failures_lock_the_source_out(self):
        for _ in range(server.LOGIN_MAX_FAILURES):
            status, _ = self.request("POST", "/api/auth/login",
                                     {"username": "admin", "password": "bad"})
            self.assertEqual(status, 401)
        # even the correct password is refused while locked out
        status, body = self.request("POST", "/api/auth/login",
                                    {"username": "admin", "password": "admin123"})
        self.assertEqual(status, 429)
        self.assertIn("retry", body["error"])


if __name__ == "__main__":
    unittest.main()
