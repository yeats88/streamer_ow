"""Build and load the OW pro player roster."""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.request
from pathlib import Path
from network import request_bytes

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PLAYERS_PATH = DATA_DIR / "players.json"
CUSTOM_PATH = DATA_DIR / "custom.json"
ROSTER_SOURCE = "https://owkitsune.com/api/gas?view=playerlinks"

ROLE_MAP = {
    "💪TANK": "tank",
    "🔫DPS": "dps",
    "💉SUP": "support",
    "🔄FLEX": "flex",
    "📋COACH": "coach",
    "🛠️MANAGER": "manager",
    "HERO": "hero",
}

REGION_LABELS = {
    "NA": "北美",
    "EMEA": "欧洲",
    "KR": "韩国",
    "CN": "中国",
    "JP": "日本",
    "PAC": "太平洋",
    "★OWCS Creator": "创作者",
    "● Team Official": "队伍官方",
    "●Esports Related": "赛事相关",
    "●OWWC Team Official": "国家队",
    "●FACEIT League": "FACEIT",
    "●Official ow esports": "OWCS官方",
    "PRIME eLeague": "PRIME",
    "": "未分区",
}


def region_label(code: str) -> str:
    return REGION_LABELS.get(code or "", code or "未分区")


def kind_for(team: str, region: str, role: str) -> str:
    team_l = (team or "").lower()
    if role == "hero" or team == "Portal:Heroes":
        return "hero"
    if "caster" in team_l or team == "Category:Casters":
        return "caster"
    if role == "coach":
        return "coach"
    if role == "manager":
        return "staff"
    if (region or "").startswith("●") or "ow esports" in team_l or "official" in team_l:
        return "broadcast"
    if "creator" in (region or "").lower():
        return "creator"
    if role in {"tank", "dps", "support", "flex"}:
        return "player"
    if role == "coach":
        return "coach"
    return "other"


def slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text.lower() if ch.isalnum())
    return text or "x"


def _login_from_twitch(url: str) -> str:
    match = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", url or "", re.I)
    if match:
        return match.group(1).lower()
    value = (url or "").strip().lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{2,25}", value):
        return value.lower()
    return ""


def _id_from_chzzk(url: str) -> str:
    match = re.search(r"chzzk\.naver\.com/(?:live/)?([a-f0-9]{32})", url or "", re.I)
    if match:
        return match.group(1)
    value = (url or "").strip()
    return value if re.fullmatch(r"[a-f0-9]{32}", value, re.I) else ""


def _id_from_soop(url: str) -> str:
    match = re.search(
        r"(?:play\.|www\.|ch\.)?(?:sooplive\.co\.kr|afreecatv\.com)/([A-Za-z0-9_]+)",
        url or "",
        re.I,
    )
    value = match.group(1) if match else (url or "").strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9_]{2,25}", value):
        return ""
    if value.lower() in {"station", "search", "live", "https", "http"}:
        return ""
    return value


def _bili_from_url(url: str) -> dict:
    space = re.search(r"space\.bilibili\.com/(\d+)", url or "")
    if space:
        return {"mid": space.group(1)}
    room = re.search(r"live\.bilibili\.com/(\d+)", url or "")
    if room:
        return {"room": room.group(1)}
    value = (url or "").strip()
    if re.fullmatch(r"\d{3,16}", value):
        return {"mid": value}
    return {}


def _youtube_from_url(url: str) -> dict:
    if not url:
        return {}
    channel = re.search(r"youtube\.com/channel/([A-Za-z0-9_-]+)", url, re.I)
    if channel:
        return {"channel": channel.group(1)}
    handle = re.search(r"youtube\.com/@([A-Za-z0-9._-]+)", url, re.I)
    if handle:
        return {"handle": handle.group(1)}
    custom = re.search(r"youtube\.com/(?:c|user)/([A-Za-z0-9._-]+)", url, re.I)
    if custom:
        return {"custom": custom.group(1)}
    value = url.strip()
    if re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", value):
        return {"channel": value}
    if value.startswith("@") and re.fullmatch(r"@[A-Za-z0-9._-]{2,40}", value):
        return {"handle": value[1:]}
    return {}


def primary_platform(url: str) -> str:
    host = (url or "").lower()
    if "twitch.tv" in host:
        return "twitch"
    if "chzzk.naver.com" in host:
        return "chzzk"
    if "sooplive" in host or "afreeca" in host:
        return "soop"
    if "bilibili.com" in host:
        return "bilibili"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return ""


def normalize_player(raw: dict, fallback_id: str = "") -> dict | None:
    name = (raw.get("name") or "").strip()
    if not name:
        return None
    team = (raw.get("team") or "").strip()
    region = (raw.get("teamRegion") or raw.get("region") or "").strip()
    role_raw = raw.get("role") or ""
    role = ROLE_MAP.get(role_raw, raw.get("roleKey") or "")
    if not role and role_raw in {"tank", "dps", "support", "flex", "coach", "manager"}:
        role = role_raw
    kind = raw.get("kind") or kind_for(team, region, role)
    if kind == "hero":
        return None

    platforms = raw.get("platforms") if isinstance(raw.get("platforms"), dict) else {}
    twitch = platforms.get("twitch") or _login_from_twitch(raw.get("twitchUrl") or raw.get("twitch") or "")
    chzzk = platforms.get("chzzk") or _id_from_chzzk(raw.get("chzzkUrl") or raw.get("chzzk") or "")
    soop = platforms.get("soop") or _id_from_soop(raw.get("soopUrl") or raw.get("soop") or "")
    bili = platforms.get("bilibili") if isinstance(platforms.get("bilibili"), dict) else {}
    if not bili:
        bili = _bili_from_url(raw.get("biliUrl") or raw.get("bilibili") or "")
    youtube = platforms.get("youtube") if isinstance(platforms.get("youtube"), dict) else {}
    if not youtube:
        youtube = _youtube_from_url(raw.get("youtubeUrl") or raw.get("youtube") or "")

    cleaned = {
        "twitch": twitch,
        "chzzk": chzzk,
        "soop": soop,
        "bilibili": bili,
        "youtube": youtube,
    }
    if not any(cleaned.values()):
        return None

    aliases = raw.get("aliases") or raw.get("playerAlias") or ""
    heroes = raw.get("heroes") or raw.get("signatureHeroes") or ""
    if heroes == "-":
        heroes = ""
    player_id = raw.get("id") or fallback_id or f"{slug(name)}-{slug(team)}"
    return {
        "id": player_id,
        "name": name,
        "team": team,
        "region": region,
        "regionLabel": region_label(region),
        "role": role,
        "kind": kind,
        "nationality": (raw.get("nationality") or raw.get("nat") or "")[:8],
        "aliases": aliases,
        "heroes": heroes,
        "primary": raw.get("primary") or primary_platform(raw.get("lastStreamUrl") or ""),
        "platforms": cleaned,
        "custom": bool(raw.get("custom")),
    }


def fetch_source_players() -> list[dict]:
    raw = request_bytes(
        ROSTER_SOURCE,
        headers={"User-Agent": "OWCSLiveMonitor/1.0 (local dashboard)", "Accept": "application/json"},
        timeout=60,
    )
    payload = json.loads(raw.decode("utf-8"))
    rows = payload.get("playerLinks") or payload.get("players") or []
    if not isinstance(rows, list):
        raise ValueError("名册响应格式无法识别")
    return rows


def build_players(rows: list[dict]) -> list[dict]:
    players = []
    seen: dict[str, int] = {}
    for raw in rows:
        base = slug(raw.get("name") or "") + "-" + slug(raw.get("team") or "")
        seen[base] = seen.get(base, 0) + 1
        player_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        player = normalize_player(raw, player_id)
        if player:
            players.append(player)
    players.sort(key=lambda item: (item["region"], item["team"].lower(), item["name"].lower()))
    return players


def save_players(players: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLAYERS_PATH.write_text(json.dumps(players, ensure_ascii=False, indent=2), encoding="utf-8")


def load_players() -> list[dict]:
    if not PLAYERS_PATH.exists():
        return []
    return json.loads(PLAYERS_PATH.read_text(encoding="utf-8"))


def load_custom() -> list[dict]:
    if not CUSTOM_PATH.exists():
        return []
    data = json.loads(CUSTOM_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def save_custom(players: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOM_PATH.write_text(json.dumps(players, ensure_ascii=False, indent=2), encoding="utf-8")


def merged_roster() -> list[dict]:
    seeded = load_players()
    custom = []
    for raw in load_custom():
        player = normalize_player({**raw, "custom": True}, raw.get("id") or "")
        if player:
            player["custom"] = True
            custom.append(player)
    by_id = {player["id"]: player for player in seeded}
    for player in custom:
        by_id[player["id"]] = player
    return list(by_id.values())


if __name__ == "__main__":
    built = build_players(fetch_source_players())
    save_players(built)
    print(f"saved {len(built)} players -> {PLAYERS_PATH}")
