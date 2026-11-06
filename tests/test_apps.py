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
    def test_saved_without_live_search(self):
        calls = []

        def prowlarr(method, path, body=None):
            calls.append((method, path))
            if path == "/indexer":
                return []
            if path == "/indexer/schema":
                return [{"definitionName": "internetarchive", "name": "x", "fields": []}]
        apps.add_default_indexer(prowlarr)
        # archive.org can take minutes to answer Prowlarr's save-time test search
        self.assertEqual(calls[-1], ("POST", "/indexer?forceSave=true"))
