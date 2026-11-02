import json
import os
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import monitor
import network
import roster


class ProxyHandler(BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        self.seen.append(("GET", self.path, self.headers.get("Proxy-Authorization")))
        body = b'{"playerLinks": [{"name": "example"}]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        self.seen.append(("CONNECT", self.path, self.headers.get("Proxy-Authorization")))
        self.send_error(502, "Test tunnel rejected")

    def log_message(self, *_args):
        pass


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "settings.json"
        for target in ("network.SETTINGS_PATH", "monitor.SETTINGS_PATH"):
            patcher = patch(target, self.path)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {"NO_PROXY": "", "no_proxy": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, **updates):
        settings = {**network.PROXY_DEFAULTS, **updates}
        self.path.write_text(json.dumps(settings), encoding="utf-8")
        return settings

    def test_existing_settings_default_to_disabled(self):
        self.path.write_text('{"intervalSec": 60}', encoding="utf-8")
        settings = monitor.load_settings()
        self.assertFalse(settings["proxyEnabled"])
        self.assertEqual(settings["intervalSec"], 60)

    def test_save_reload_preserve_secret_and_disable(self):
        result = monitor.save_settings({
            "proxyEnabled": True,
            "proxyUrl": "http://user:secret@127.0.0.1:7890",
            "proxyPlatforms": ["twitch", "roster"],
        })
        self.assertTrue(result["proxyConfigured"])
        self.assertNotIn("proxyUrl", result)
        self.assertNotIn("secret", json.dumps(result))
        monitor.save_settings({"proxyUrl": "", "intervalSec": 60})
        self.assertEqual(network.proxy_settings()["proxyUrl"], "http://user:secret@127.0.0.1:7890")
        monitor.save_settings({"proxyEnabled": False, "proxyPlatforms": []})
        self.assertFalse(network.proxy_settings()["proxyEnabled"])
        self.assertEqual(network.proxy_settings()["proxyPlatforms"], [])

    def test_invalid_settings_do_not_overwrite_file(self):
        self.save()
        before = self.path.read_bytes()
        for updates in (
            {"proxyEnabled": True},
            {"proxyUrl": "socks5://127.0.0.1:1080"},
            {"proxyUrl": "http://host:99999"},
            {"proxyUrl": "http://host/path"},
            {"proxyUrl": 123},
            {"proxyEnabled": "false"},
            {"proxyPlatforms": ["invalid"]},
            {"proxyPlatforms": "twitch"},
            {"proxyPlatforms": [{}]},
        ):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                monitor.save_settings(updates)
            self.assertEqual(self.path.read_bytes(), before)

    def test_all_platforms_route_and_unselected_is_direct(self):
        self.save(proxyEnabled=True, proxyUrl="http://127.0.0.1:7890")
        for host in (
            "gql.twitch.tv", "api.twitch.tv", "id.twitch.tv",
            "api.chzzk.naver.com", "chapi.sooplive.co.kr",
            "www.youtube.com", "owkitsune.com",
        ):
            with self.subTest(host=host), patch("network.urllib.request.build_opener") as build:
                network.request_bytes(f"https://{host}/", data=b"body", headers={"X-Test": "yes"}, timeout=7)
                handler = build.call_args.args[0]
                self.assertEqual(handler.proxies["https"], "http://127.0.0.1:7890")
                request = build.return_value.open.call_args.args[0]
                self.assertEqual(request.data, b"body")
                self.assertEqual(request.get_header("X-test"), "yes")
                self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 7)
        for host in ("api.live.bilibili.com", "twitch.tv.example.org"):
            with patch("network.urllib.request.build_opener") as build:
                network.request_bytes(f"https://{host}/")
                self.assertEqual(build.call_args.args[0].proxies, {})
        self.save(proxyEnabled=True, proxyUrl="http://127.0.0.1:7890", proxyPlatforms=["bilibili"])
        with patch("network.urllib.request.build_opener") as build:
            network.request_bytes("https://api.live.bilibili.com/")
            self.assertIn("https", build.call_args.args[0].proxies)

    def test_disabled_preserves_urlopen(self):
        self.save()
        with patch("network.urllib.request.urlopen") as open_url:
            network.request_bytes("https://api.twitch.tv/", timeout=9)
            open_url.assert_called_once()
            self.assertEqual(open_url.call_args.kwargs["timeout"], 9)

    def test_roster_and_monitor_use_shared_client(self):
        with patch("roster.request_bytes", return_value=b'{"playerLinks": []}') as request:
            self.assertEqual(roster.fetch_source_players(), [])
            self.assertEqual(request.call_args.kwargs["timeout"], 60)
        with patch("monitor.request_bytes", return_value=b"ok") as request:
            self.assertEqual(monitor._request("https://api.twitch.tv/"), b"ok")
            request.assert_called_once()

    def test_real_http_proxy_and_https_connect_without_direct_fallback(self):
        ProxyHandler.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.save(
                proxyEnabled=True,
                proxyUrl=f"http://user:password@127.0.0.1:{server.server_port}",
                proxyPlatforms=["twitch", "roster"],
            )
            result = network.request_bytes("http://api.twitch.tv/example", timeout=2)
            self.assertIn(b"playerLinks", result)
            self.assertEqual(ProxyHandler.seen[0][:2], ("GET", "http://api.twitch.tv/example"))
            self.assertTrue(ProxyHandler.seen[0][2].startswith("Basic "))
            with self.assertRaises(urllib.error.URLError):
                network.request_bytes("https://api.twitch.tv/example", timeout=2)
            self.assertEqual(ProxyHandler.seen[-1][:2], ("CONNECT", "api.twitch.tv:443"))
            self.assertEqual(len(ProxyHandler.seen), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
