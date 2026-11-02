"""Local dashboard for OWCS / Overwatch pro stream status."""

from __future__ import annotations

import json
import mimetypes
import sys
import threading

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from monitor import LiveMonitor, compose_player, load_settings, public_settings, save_settings
from roster import (
    CUSTOM_PATH,
    build_players,
    fetch_source_players,
    load_custom,
    merged_roster,
    normalize_player,
    save_custom,
    save_players,
    slug,
)

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
HIDDEN_PATH = ROOT / "data" / "hidden.json"
HOST = "127.0.0.1"
PORT = 8787

monitor = LiveMonitor()
roster_lock = threading.Lock()


def load_hidden() -> set[str]:
    if not HIDDEN_PATH.exists():
        return set()
    try:
        data = json.loads(HIDDEN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(data if isinstance(data, list) else [])


def save_hidden(ids: set[str]) -> None:
    HIDDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    HIDDEN_PATH.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8")


def public_players(include_hidden: bool = False) -> list[dict]:
    snap = monitor.snapshot()
    hidden = load_hidden()
    players = []
    for player in merged_roster():
        if player["id"] in hidden and not include_hidden:
            continue
        composed = compose_player(player, snap["platformStatus"])
        composed["hidden"] = player["id"] in hidden
        players.append(composed)
    players.sort(key=lambda item: ((item.get("viewers") or 0) * -1, item["name"].lower()))
    return players


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        if str(args[1:2]) not in {"('404',)", "('304',)"}:
            super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/players":
            self._json({"ok": True, **self._status_payload(), "players": public_players()})
            return
        if parsed.path == "/api/meta":
            self._json(self._meta())
            return
        if parsed.path == "/api/health":
            self._json({"ok": True, **self._status_payload()})
            return
        if parsed.path in {"/", "/index.html"}:
            self._file(STATIC / "index.html")
            return
        target = (STATIC / parsed.path.lstrip("/")).resolve()
        if STATIC in target.parents and target.is_file():
            self._file(target)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._body()
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        if parsed.path == "/api/players":
            self._add_player(body)
            return
        if parsed.path == "/api/hidden":
            self._hide(body)
            return
        if parsed.path == "/api/settings":
            try:
                saved = save_settings(body)
            except (TypeError, ValueError) as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
                return
            self._json({"ok": True, "settings": saved})
            return
        if parsed.path == "/api/roster/refresh":
            self._refresh_roster()
            return
        if parsed.path == "/api/refresh":
            threading.Thread(target=monitor.refresh, daemon=True).start()
            self._json({"ok": True, "started": True})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        prefix = "/api/players/"
        if not parsed.path.startswith(prefix):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        player_id = parsed.path[len(prefix) :]
        custom = [item for item in load_custom() if item.get("id") != player_id]
        if len(custom) == len(load_custom()):
            self._json({"ok": False, "error": "只能删除手动添加的选手"}, 400)
            return
        save_custom(custom)
        self._json({"ok": True})

    def _status_payload(self) -> dict:
        snap = monitor.snapshot()
        return {
            "updatedAt": snap["updatedAt"],
            "checking": snap["checking"],
            "errors": snap["errors"],
            "settings": public_settings(),
        }

    def _meta(self) -> dict:
        players = public_players()
        regions = {}
        teams = {}
        for player in players:
            regions[player["region"] or ""] = player["regionLabel"]
            if player["team"]:
                teams[player["team"]] = teams.get(player["team"], 0) + 1
        return {
            "ok": True,
            **self._status_payload(),
            "total": len(players),
            "live": sum(1 for player in players if player["state"] == "live"),
            "regions": [{"id": key, "label": label} for key, label in sorted(regions.items(), key=lambda item: item[1])],
            "teams": [name for name, _count in sorted(teams.items(), key=lambda item: (-item[1], item[0].lower()))],
        }

    def _add_player(self, body: dict) -> None:
        name = (body.get("name") or "").strip()
        if not name:
            self._json({"ok": False, "error": "需要选手名"}, 400)
            return
        player_id = body.get("id") or f"custom-{slug(name)}-{slug(body.get('team') or '')}"
        raw = {
            "id": player_id,
            "name": name,
            "team": (body.get("team") or "").strip(),
            "region": (body.get("region") or "").strip(),
            "roleKey": (body.get("role") or "").strip(),
            "kind": "player",
            "custom": True,
            "twitch": (body.get("twitch") or "").strip(),
            "chzzk": (body.get("chzzk") or "").strip(),
            "soop": (body.get("soop") or "").strip(),
            "bilibili": (body.get("bilibili") or "").strip(),
            "youtube": (body.get("youtube") or "").strip(),
        }
        player = normalize_player(raw, player_id)
        if not player:
            self._json({"ok": False, "error": "至少填写一个直播平台链接或 ID"}, 400)
            return
        player["custom"] = True
        with roster_lock:
            custom = [item for item in load_custom() if item.get("id") != player["id"]]
            custom.append(player)
            save_custom(custom)
        threading.Thread(target=monitor.refresh, daemon=True).start()
        self._json({"ok": True, "player": player})

    def _hide(self, body: dict) -> None:
        player_id = (body.get("id") or "").strip()
        if not player_id:
            self._json({"ok": False, "error": "缺少 id"}, 400)
            return
        hidden = load_hidden()
        if body.get("hidden", True):
            hidden.add(player_id)
        else:
            hidden.discard(player_id)
        save_hidden(hidden)
        self._json({"ok": True})

    def _refresh_roster(self) -> None:
        try:
            players = build_players(fetch_source_players())
            save_players(players)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": f"名册刷新失败: {exc}"}, 502)
            return
        threading.Thread(target=monitor.refresh, daemon=True).start()
        self._json({"ok": True, "count": len(players)})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            raise ValueError("请求过大")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("需要 JSON 对象")
        return data

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        body = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
            mime += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    CUSTOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not (ROOT / "data" / "players.json").exists():
        print("名册不存在，正在下载公开选手资料...")
        save_players(build_players(fetch_source_players()))
    monitor.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"OW 直播监控已启动: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        monitor.stop()
        server.server_close()


if __name__ == "__main__":
    main()
