import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from installer import apps


class Slow(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(1)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class CallTest(unittest.TestCase):
    def test_timeout_names_the_url(self):
        server = HTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/api/v1/indexer"
        old, apps.TIMEOUT = apps.TIMEOUT, 0.2
        self.addCleanup(setattr, apps, "TIMEOUT", old)
        with self.assertRaises(OSError) as ctx:
            apps.call("GET", url)
        self.assertIn("/api/v1/indexer", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class DefaultIndexerTest(unittest.TestCase):
    def test_saved_disabled_then_bulk_enabled(self):
        calls = []

        def prowlarr(method, path, body=None):
            calls.append((method, path, body))
            if path == "/indexer":
                return [] if method == "GET" else {"id": 7}
            if path == "/indexer/schema":
                return [{"definitionName": "internetarchive", "name": "x", "fields": []}]
        apps.add_default_indexer(prowlarr)
        # Saving an enabled indexer runs a live search that archive.org can take minutes to answer;
        # a disabled one saves at once, and the bulk edit enables it without testing.
        post = next(c for c in calls if c[0] == "POST")
        self.assertEqual((post[1], post[2]["enable"]), ("/indexer", False))
        self.assertEqual(calls[-1], ("PUT", "/indexer/bulk", {"ids": [7], "enable": True}))
