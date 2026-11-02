"""Poll Twitch, CHZZK, SOOP, Bilibili, and YouTube for live status."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from roster import DATA_DIR, merged_roster

CACHE_PATH = DATA_DIR / "status_cache.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
TWITCH_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
DEFAULT_SETTINGS = {"intervalSec": 45, "youtubeMode": "primary"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_PATH.exists():
        try:
            stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update(stored)
        except json.JSONDecodeError:
            pass
    if os.environ.get("TWITCH_CLIENT_ID"):
        settings["twitchClientId"] = os.environ["TWITCH_CLIENT_ID"]
    if os.environ.get("TWITCH_CLIENT_SECRET"):
        settings["twitchClientSecret"] = os.environ["TWITCH_CLIENT_SECRET"]
    settings["intervalSec"] = max(20, min(int(settings.get("intervalSec") or 45), 300))
    if settings.get("youtubeMode") not in {"off", "primary", "all"}:
        settings["youtubeMode"] = "primary"
    return settings


def save_settings(updates: dict) -> dict:
    current = load_settings()
    if "intervalSec" in updates and str(updates["intervalSec"]).strip():
        current["intervalSec"] = int(updates["intervalSec"])
    if updates.get("youtubeMode") in {"off", "primary", "all"}:
        current["youtubeMode"] = updates["youtubeMode"]
    if str(updates.get("twitchClientId") or "").strip():
        current["twitchClientId"] = str(updates["twitchClientId"]).strip()
    if str(updates.get("twitchClientSecret") or "").strip():
        current["twitchClientSecret"] = str(updates["twitchClientSecret"]).strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return public_settings(current)


def public_settings(settings: dict | None = None) -> dict:
    settings = settings or load_settings()
    return {
        "intervalSec": settings["intervalSec"],
        "youtubeMode": settings["youtubeMode"],
        "twitchConfigured": bool(settings.get("twitchClientId") and settings.get("twitchClientSecret")),
    }


def _request(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 12) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": "OWCSLiveMonitor/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class LiveMonitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._refreshing = False
        self.platform_status: dict[str, dict] = {}
        self.updated_at = ""
        self.errors: list[str] = []
        self._helix_token = ""
        self._helix_expires = 0.0
        self._load_cache()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="live-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "updatedAt": self.updated_at,
                "checking": self._refreshing,
                "errors": list(self.errors),
                "platformStatus": dict(self.platform_status),
            }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                with self._lock:
                    self.errors = [f"刷新失败: {exc}"][:8]
            interval = load_settings()["intervalSec"]
            self._stop.wait(interval)

    def refresh(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        try:
            players = merged_roster()
            settings = load_settings()
            statuses, errors = self._collect(players, settings)
            with self._lock:
                self.platform_status.update(statuses)
                self.updated_at = utc_now()
                self.errors = errors
            self._save_cache()
        finally:
            with self._lock:
                self._refreshing = False

    def _collect(self, players: list[dict], settings: dict) -> tuple[dict[str, dict], list[str]]:
        twitch_logins = sorted({p["platforms"]["twitch"] for p in players if p["platforms"].get("twitch")})
        chzzk_ids = sorted({p["platforms"]["chzzk"] for p in players if p["platforms"].get("chzzk")})
        soop_ids = sorted({p["platforms"]["soop"] for p in players if p["platforms"].get("soop")})
        bili = []
        youtube = []
        for player in players:
            bili_info = player["platforms"].get("bilibili") or {}
            if bili_info:
                bili.append((bili_key(bili_info), bili_info))
            if should_check_youtube(player, settings["youtubeMode"]):
                yt = player["platforms"].get("youtube") or {}
                if yt:
                    youtube.append((youtube_key(yt), yt))

        statuses: dict[str, dict] = {}
        errors: list[str] = []
        jobs = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs.append(pool.submit(self._twitch_many, twitch_logins, settings))
            jobs.append(pool.submit(self._many, chzzk_ids, "chzzk", self._chzzk_one, 8))
            jobs.append(pool.submit(self._many, soop_ids, "soop", self._soop_one, 6))
            jobs.append(pool.submit(self._many_pairs, bili, "bilibili", self._bili_one, 6))
            jobs.append(pool.submit(self._many_pairs, youtube, "youtube", self._youtube_one, 4))
            for job in as_completed(jobs):
                part, error = job.result()
                statuses.update(part)
                if error:
                    errors.append(error)
        return statuses, errors[:8]

    def _many(self, ids: list[str], prefix: str, fn, workers: int) -> tuple[dict, str]:
        return self._many_pairs([(f"{prefix}:{item}", item) for item in ids], prefix, fn, workers)

    def _many_pairs(self, items: list[tuple[str, object]], prefix: str, fn, workers: int) -> tuple[dict, str]:
        found: dict[str, dict] = {}
        failures = 0
        if not items:
            return found, ""
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fn, item): key for key, item in items}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    found[key] = future.result()
                except Exception:
                    failures += 1
                    found[key] = {"live": None, "platform": prefix, "error": True}
        error = f"{prefix} 有 {failures} 个频道暂时查不到" if failures else ""
        return found, error

    def _twitch_many(self, logins: list[str], settings: dict) -> tuple[dict, str]:
        found: dict[str, dict] = {}
        if not logins:
            return found, ""
        try:
            if settings.get("twitchClientId") and settings.get("twitchClientSecret"):
                found.update(self._twitch_helix(logins, settings))
            else:
                found.update(self._twitch_gql(logins))
            return found, ""
        except Exception as exc:  # noqa: BLE001
            return found, f"Twitch 查询失败: {exc}"

    def _twitch_gql(self, logins: list[str]) -> dict[str, dict]:
        found: dict[str, dict] = {}
        query = """
        query($logins:[String!]!){
          users(logins:$logins){
            login
            stream { title viewersCount createdAt game { name } }
          }
        }
        """
        for chunk in chunks(logins, 50):
            body = json.dumps({"query": query, "variables": {"logins": chunk}}).encode()
            raw = _request(
                "https://gql.twitch.tv/gql",
                data=body,
                headers={
                    "Client-ID": TWITCH_CLIENT_ID,
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            payload = json.loads(raw.decode())
            seen = set()
            for user in payload.get("data", {}).get("users") or []:
                if not user:
                    continue
                login = user["login"].lower()
                seen.add(login)
                found[f"twitch:{login}"] = twitch_status(login, user.get("stream"))
            for login in chunk:
                found.setdefault(f"twitch:{login}", {"live": False, "platform": "twitch", "login": login})
            missing = [login for login in chunk if login not in seen]
            for login in missing:
                found[f"twitch:{login}"] = {"live": False, "platform": "twitch", "login": login, "missing": True}
        return found

    def _helix_token(self, settings: dict) -> str:
        if self._helix_token and time.time() < self._helix_expires - 60:
            return self._helix_token
        body = urllib.parse.urlencode(
            {
                "client_id": settings["twitchClientId"],
                "client_secret": settings["twitchClientSecret"],
                "grant_type": "client_credentials",
            }
        ).encode()
        raw = _request("https://id.twitch.tv/oauth2/token", data=body)
        payload = json.loads(raw.decode())
        self._helix_token = payload["access_token"]
        self._helix_expires = time.time() + int(payload.get("expires_in") or 3600)
        return self._helix_token

    def _twitch_helix(self, logins: list[str], settings: dict) -> dict[str, dict]:
        token = self._helix_token(settings)
        found: dict[str, dict] = {}
        live_logins = set()
        for chunk in chunks(logins, 100):
            query = urllib.parse.urlencode([("user_login", login) for login in chunk])
            raw = _request(
                f"https://api.twitch.tv/helix/streams?{query}",
                headers={
                    "Client-ID": settings["twitchClientId"],
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "OWCSLiveMonitor/1.0",
                },
            )
            for stream in json.loads(raw.decode()).get("data") or []:
                login = stream["user_login"].lower()
                live_logins.add(login)
                found[f"twitch:{login}"] = {
                    "live": True,
                    "platform": "twitch",
                    "login": login,
                    "title": stream.get("title") or "",
                    "viewers": stream.get("viewer_count") or 0,
                    "game": stream.get("game_name") or "",
                    "startedAt": stream.get("started_at") or "",
                    "url": f"https://www.twitch.tv/{login}",
                    "thumbnail": f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-440x248.jpg",
                }
        for login in logins:
            found.setdefault(
                f"twitch:{login}",
                {"live": False, "platform": "twitch", "login": login, "url": f"https://www.twitch.tv/{login}"},
            )
        return found

    def _chzzk_one(self, channel_id: str) -> dict:
        raw = _request(
            f"https://api.chzzk.naver.com/service/v3/channels/{channel_id}/live-detail",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        content = json.loads(raw.decode()).get("content")
        url = f"https://chzzk.naver.com/live/{channel_id}"
        if not content or content.get("status") != "OPEN":
            return {"live": False, "platform": "chzzk", "url": url}
        image = (content.get("liveImageUrl") or "").replace("{type}", "480")
        return {
            "live": True,
            "platform": "chzzk",
            "title": content.get("liveTitle") or "",
            "viewers": content.get("concurrentUserCount") or 0,
            "game": content.get("liveCategoryValue") or "",
            "startedAt": content.get("openDate") or "",
            "url": url,
            "thumbnail": image,
        }

    def _soop_one(self, bjid: str) -> dict:
        raw = _request(
            f"https://chapi.sooplive.co.kr/api/{urllib.parse.quote(bjid)}/station",
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Referer": "https://play.sooplive.co.kr/",
            },
        )
        payload = json.loads(raw.decode())
        broad = payload.get("broad") or {}
        url = f"https://play.sooplive.co.kr/{bjid}"
        if not broad or not broad.get("broad_no"):
            return {"live": False, "platform": "soop", "url": url}
        return {
            "live": True,
            "platform": "soop",
            "title": broad.get("broad_title") or "",
            "viewers": broad.get("current_sum_viewer") or 0,
            "url": url,
            "thumbnail": f"https://liveimg.sooplive.co.kr/m/{broad['broad_no']}.webp",
        }

    def _bili_one(self, info: dict) -> dict:
        if info.get("room"):
            raw = _request(
                f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={info['room']}",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"},
            )
            data = json.loads(raw.decode()).get("data") or {}
            live = data.get("live_status") == 1
            room = data.get("room_id") or info["room"]
            return {
                "live": live,
                "platform": "bilibili",
                "title": data.get("title") or "",
                "viewers": data.get("online") or 0,
                "url": f"https://live.bilibili.com/{room}",
                "thumbnail": data.get("user_cover") or data.get("cover") or "",
            }
        raw = _request(
            f"https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid={info['mid']}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"},
        )
        data = json.loads(raw.decode()).get("data") or {}
        return {
            "live": data.get("liveStatus") == 1,
            "platform": "bilibili",
            "title": data.get("title") or "",
            "viewers": data.get("online") or 0,
            "url": data.get("url") or f"https://space.bilibili.com/{info['mid']}",
            "thumbnail": data.get("cover") or "",
        }

    def _youtube_one(self, info: dict) -> dict:
        if info.get("channel"):
            target = f"https://www.youtube.com/channel/{info['channel']}/live"
        elif info.get("handle"):
            target = f"https://www.youtube.com/@{info['handle']}/live"
        else:
            target = f"https://www.youtube.com/{info.get('custom')}/live"
        query = urllib.parse.urlencode({"url": target, "format": "json"})
        try:
            raw = _request(
                f"https://www.youtube.com/oembed?{query}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=8,
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"live": False, "platform": "youtube", "url": target}
            raise
        payload = json.loads(raw.decode())
        return {
            "live": True,
            "platform": "youtube",
            "title": payload.get("title") or "",
            "viewers": None,
            "url": payload.get("author_url") or target,
            "thumbnail": payload.get("thumbnail_url") or "",
        }

    def _load_cache(self) -> None:
        if not CACHE_PATH.exists():
            return
        try:
            payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        self.platform_status = payload.get("platformStatus") or {}
        self.updated_at = payload.get("updatedAt") or ""

    def _save_cache(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"updatedAt": self.updated_at, "platformStatus": self.platform_status}
        CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def chunks(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def twitch_status(login: str, stream: dict | None) -> dict:
    url = f"https://www.twitch.tv/{login}"
    if not stream:
        return {"live": False, "platform": "twitch", "login": login, "url": url}
    return {
        "live": True,
        "platform": "twitch",
        "login": login,
        "title": stream.get("title") or "",
        "viewers": stream.get("viewersCount") or 0,
        "game": (stream.get("game") or {}).get("name") or "",
        "startedAt": stream.get("createdAt") or "",
        "url": url,
        "thumbnail": f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-440x248.jpg",
    }


def bili_key(info: dict) -> str:
    if info.get("room"):
        return f"bilibili:room:{info['room']}"
    return f"bilibili:mid:{info.get('mid')}"


def youtube_key(info: dict) -> str:
    if info.get("channel"):
        return f"youtube:channel:{info['channel']}"
    if info.get("handle"):
        return f"youtube:handle:{info['handle']}"
    return f"youtube:custom:{info.get('custom')}"


def should_check_youtube(player: dict, mode: str) -> bool:
    if mode == "off":
        return False
    yt = player["platforms"].get("youtube") or {}
    if not yt:
        return False
    if mode == "all":
        return True
    others = [player["platforms"].get(name) for name in ("twitch", "chzzk", "soop", "bilibili")]
    return player.get("primary") == "youtube" or not any(others)


def platform_keys(player: dict) -> list[str]:
    keys = []
    platforms = player["platforms"]
    if platforms.get("twitch"):
        keys.append(f"twitch:{platforms['twitch']}")
    if platforms.get("chzzk"):
        keys.append(f"chzzk:{platforms['chzzk']}")
    if platforms.get("soop"):
        keys.append(f"soop:{platforms['soop']}")
    if platforms.get("bilibili"):
        keys.append(bili_key(platforms["bilibili"]))
    if platforms.get("youtube"):
        keys.append(youtube_key(platforms["youtube"]))
    return keys


def compose_player(player: dict, platform_status: dict) -> dict:
    lives = []
    checked = 0
    unknown = 0
    for key in platform_keys(player):
        status = platform_status.get(key)
        if not status:
            unknown += 1
            continue
        if status.get("error") or status.get("live") is None:
            unknown += 1
            continue
        checked += 1
        if status.get("live"):
            lives.append(status)
    lives.sort(key=lambda item: item.get("viewers") or 0, reverse=True)
    best = lives[0] if lives else None
    if lives:
        state = "live"
    elif checked:
        state = "offline"
    else:
        state = "unknown"
    links = {}
    platforms = player["platforms"]
    if platforms.get("twitch"):
        links["twitch"] = f"https://www.twitch.tv/{platforms['twitch']}"
    if platforms.get("chzzk"):
        links["chzzk"] = f"https://chzzk.naver.com/{platforms['chzzk']}"
    if platforms.get("soop"):
        links["soop"] = f"https://play.sooplive.co.kr/{platforms['soop']}"
    bili = platforms.get("bilibili") or {}
    if bili.get("room"):
        links["bilibili"] = f"https://live.bilibili.com/{bili['room']}"
    elif bili.get("mid"):
        links["bilibili"] = f"https://space.bilibili.com/{bili['mid']}"
    yt = platforms.get("youtube") or {}
    if yt.get("channel"):
        links["youtube"] = f"https://www.youtube.com/channel/{yt['channel']}"
    elif yt.get("handle"):
        links["youtube"] = f"https://www.youtube.com/@{yt['handle']}"
    elif yt.get("custom"):
        links["youtube"] = f"https://www.youtube.com/{yt['custom']}"
    return {
        **{k: player[k] for k in ("id", "name", "team", "region", "regionLabel", "role", "kind", "nationality", "aliases", "heroes", "primary", "custom")},
        "links": links,
        "state": state,
        "viewers": best.get("viewers") if best else 0,
        "title": best.get("title") if best else "",
        "game": best.get("game") if best else "",
        "startedAt": best.get("startedAt") if best else "",
        "thumbnail": best.get("thumbnail") if best else "",
        "watchUrl": best.get("url") if best else (links.get(player.get("primary")) or next(iter(links.values()), "")),
        "livePlatforms": [item.get("platform") for item in lives],
    }
