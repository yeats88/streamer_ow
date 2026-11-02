"""Shared proxy routing for platform queries and roster downloads."""

import json
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

SETTINGS_PATH = Path(__file__).resolve().parent / "data" / "settings.json"
PROXY_DEFAULTS = {
    "proxyEnabled": False,
    "proxyUrl": "",
    "proxyPlatforms": ["twitch", "chzzk", "soop", "youtube", "roster"],
}
PLATFORM_HOSTS = {
    "twitch": ("twitch.tv",),
    "chzzk": ("chzzk.naver.com",),
    "soop": ("sooplive.co.kr", "afreecatv.com"),
    "bilibili": ("bilibili.com",),
    "youtube": ("youtube.com",),
    "roster": ("owkitsune.com",),
}


def validate_proxy(settings: dict) -> None:
    if not isinstance(settings["proxyEnabled"], bool):
        raise ValueError("代理开关必须是布尔值")
    platforms = settings["proxyPlatforms"]
    if not isinstance(platforms, list) or any(
        not isinstance(item, str) or item not in PLATFORM_HOSTS for item in platforms
    ):
        raise ValueError("代理平台列表无效")
    url = settings["proxyUrl"]
    if not isinstance(url, str):
        raise ValueError("代理地址必须是字符串")
    if settings["proxyEnabled"] and not url:
        raise ValueError("启用代理时需要填写代理地址")
    if not url:
        return
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError("代理地址或端口无效") from None
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or port == 0
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in url)
    ):
        raise ValueError("请填写 HTTP 代理地址，例如 http://127.0.0.1:7890；暂不支持 SOCKS")


def proxy_settings() -> dict:
    settings = dict(PROXY_DEFAULTS)
    if SETTINGS_PATH.exists():
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(stored, dict):
            raise ValueError("设置文件必须是 JSON 对象")
        settings.update({key: stored[key] for key in PROXY_DEFAULTS if key in stored})
    validate_proxy(settings)
    return settings


def request_bytes(
    url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 12
) -> bytes:
    settings = proxy_settings()
    request = urllib.request.Request(
        url, data=data, headers=headers or {"User-Agent": "OWCSLiveMonitor/1.0"}
    )
    if not settings["proxyEnabled"]:
        # Preserve urllib's existing environment/system proxy behavior.
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    host = (urlsplit(url).hostname or "").lower()
    use_proxy = any(
        host == domain or host.endswith("." + domain)
        for platform in settings["proxyPlatforms"]
        for domain in PLATFORM_HOSTS[platform]
    )
    proxies = (
        {"http": settings["proxyUrl"], "https": settings["proxyUrl"]} if use_proxy else {}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    with opener.open(request, timeout=timeout) as response:
        return response.read()
