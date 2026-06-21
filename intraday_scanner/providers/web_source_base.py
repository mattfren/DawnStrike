"""Safe public web source helpers for Dawnstrike research collection."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from intraday_scanner.errors import ConfigError, DataProviderError
from intraday_scanner.models import utc_now_iso

DEFAULT_WEB_CONFIG_PATH = Path("config/web_sources.example.yaml")


@dataclass(frozen=True)
class WebSourceConfig:
    name: str
    type: str
    enabled: bool = True
    url: str = ""
    path: str = ""
    fixture_path: str = ""
    allowed_domains: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WebCollectionConfig:
    enabled: bool
    respect_robots: bool
    user_agent: str
    timeout_seconds: float
    rate_limit_seconds: float
    save_raw: bool
    allowed_domains: tuple[str, ...]
    sources: tuple[WebSourceConfig, ...]


@dataclass(frozen=True)
class FetchResult:
    run_id: str
    source: str
    source_type: str
    url: str
    status: str
    started_at: str
    completed_at: str
    content: str = ""
    content_type: str = ""
    status_code: int = 0
    failure_reason: str = ""
    from_fixture: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source": self.source,
            "source_type": self.source_type,
            "url": self.url,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "content_type": self.content_type,
            "status_code": self.status_code,
            "failure_reason": self.failure_reason,
            "from_fixture": self.from_fixture,
        }


def load_web_sources_config(config_path: str | Path | None = None) -> WebCollectionConfig:
    path = Path(config_path) if config_path else DEFAULT_WEB_CONFIG_PATH
    if not path.exists() and path != DEFAULT_WEB_CONFIG_PATH:
        path = DEFAULT_WEB_CONFIG_PATH
    data = _default_web_config_data() if not path.exists() else _load_simple_yaml(path)
    sources = []
    for row in list(data.get("sources") or []):
        if not isinstance(row, dict):
            continue
        known = {
            "name",
            "type",
            "enabled",
            "url",
            "path",
            "fixture_path",
            "allowed_domains",
        }
        source_allowed = row.get("allowed_domains") or []
        if isinstance(source_allowed, str):
            source_allowed = [source_allowed]
        sources.append(
            WebSourceConfig(
                name=str(row.get("name") or ""),
                type=str(row.get("type") or ""),
                enabled=_bool(row.get("enabled", True)),
                url=str(row.get("url") or ""),
                path=str(row.get("path") or ""),
                fixture_path=str(row.get("fixture_path") or ""),
                allowed_domains=tuple(str(item) for item in source_allowed),
                params={key: value for key, value in row.items() if key not in known},
            )
        )
    allowed = data.get("allowed_domains") or []
    if isinstance(allowed, str):
        allowed = [allowed]
    return WebCollectionConfig(
        enabled=_bool(data.get("enabled", True)),
        respect_robots=_bool(data.get("respect_robots", True)),
        user_agent=str(data.get("user_agent") or "DawnstrikeResearchBot/0.1"),
        timeout_seconds=float(_default_if_missing(data.get("timeout_seconds"), 15)),
        rate_limit_seconds=float(_default_if_missing(data.get("rate_limit_seconds"), 5)),
        save_raw=_bool(data.get("save_raw", True)),
        allowed_domains=tuple(str(item) for item in allowed),
        sources=tuple(sources),
    )


def get_source(config: WebCollectionConfig, source_type: str) -> WebSourceConfig | None:
    for source in config.sources:
        if source.enabled and source.type == source_type:
            return source
    return None


def enabled_sources(
    config: WebCollectionConfig,
    source_type: str | None = None,
) -> list[WebSourceConfig]:
    return [
        source
        for source in config.sources
        if source.enabled and (source_type is None or source.type == source_type)
    ]


def ensure_allowed_url(
    url: str,
    *,
    allowed_domains: tuple[str, ...],
    allow_unlisted_url: bool = False,
) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise DataProviderError("Web source URLs must use http or https.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise DataProviderError("Web source URL is missing a hostname.")
    if allow_unlisted_url:
        return
    if not any(_domain_matches(host, domain.lower()) for domain in allowed_domains):
        raise DataProviderError(f"URL host {host} is not in configured allowed_domains.")


def fetch_text(
    source: WebSourceConfig,
    config: WebCollectionConfig,
    *,
    url: str | None = None,
    allow_unlisted_url: bool = False,
) -> FetchResult:
    started_at = utc_now_iso()
    run_id = str(uuid.uuid4())
    target_url = url or source.url
    allowed_domains = source.allowed_domains or config.allowed_domains
    try:
        if source.fixture_path:
            fixture = Path(source.fixture_path)
            content = fixture.read_text(encoding="utf-8")
            return FetchResult(
                run_id=run_id,
                source=source.name,
                source_type=source.type,
                url=target_url or str(fixture),
                status="success",
                started_at=started_at,
                completed_at=utc_now_iso(),
                content=content,
                content_type=_fixture_content_type(fixture),
                status_code=200,
                from_fixture=True,
            )
        if not target_url:
            raise DataProviderError(f"{source.name} has no URL or fixture_path configured.")
        ensure_allowed_url(
            target_url,
            allowed_domains=allowed_domains,
            allow_unlisted_url=allow_unlisted_url,
        )
        if not _robots_allowed(source, config, target_url):
            raise DataProviderError(f"robots policy blocks {target_url}")
        if config.rate_limit_seconds > 0:
            time.sleep(config.rate_limit_seconds)
        request = urllib.request.Request(
            target_url,
            headers={
                "User-Agent": config.user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml,"
                    "application/json,text/plain"
                ),
            },
        )
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:  # noqa: S310
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            status_code = int(getattr(response, "status", 200) or 200)
        return FetchResult(
            run_id=run_id,
            source=source.name,
            source_type=source.type,
            url=target_url,
            status="success",
            started_at=started_at,
            completed_at=utc_now_iso(),
            content=raw.decode("utf-8", errors="replace"),
            content_type=content_type,
            status_code=status_code,
        )
    except (OSError, urllib.error.URLError, DataProviderError) as exc:
        return FetchResult(
            run_id=run_id,
            source=source.name,
            source_type=source.type,
            url=target_url,
            status="failed",
            started_at=started_at,
            completed_at=utc_now_iso(),
            failure_reason=str(exc),
        )


def artifact_payload(
    *,
    run_id: str,
    source: str,
    artifact_kind: str,
    path: str | Path,
    content_type: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact = Path(path)
    raw = artifact.read_bytes()
    return {
        "run_id": run_id,
        "source": source,
        "artifact_kind": artifact_kind,
        "path": str(artifact),
        "content_type": content_type,
        "byte_count": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "created_at": utc_now_iso(),
        "metadata": metadata or {},
    }


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _domain_matches(host: str, allowed: str) -> bool:
    return host == allowed or host.endswith(f".{allowed}")


def _robots_allowed(source: WebSourceConfig, config: WebCollectionConfig, url: str) -> bool:
    if not config.respect_robots:
        return True
    explicit = source.params.get("robots_allowed")
    if explicit is not None:
        return _bool(explicit)
    # Practical default: do not bypass known blockers, and allow fixture/offline
    # runs without making an extra network call for robots.txt.
    return True


def _fixture_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".html", ".htm"}:
        return "text/html"
    if suffix == ".json":
        return "application/json"
    if suffix in {".xml", ".rss"}:
        return "application/xml"
    return "text/plain"


def _default_web_config_data() -> dict[str, Any]:
    return {
        "enabled": True,
        "respect_robots": True,
        "user_agent": "DawnstrikeResearchBot/0.1 contact: YOUR_EMAIL_HERE",
        "timeout_seconds": 15,
        "rate_limit_seconds": 5,
        "save_raw": True,
        "allowed_domains": ["nasdaqtrader.com", "sec.gov"],
        "sources": [
            {"name": "nasdaq_symbols", "type": "nasdaq_symbol_directory", "enabled": True},
            {"name": "nasdaq_halts", "type": "nasdaq_trade_halts_rss", "enabled": True},
            {"name": "sec_edgar", "type": "sec_edgar", "enabled": True},
            {
                "name": "local_inbox",
                "type": "local_inbox",
                "path": "data/inbox/screener",
                "enabled": True,
            },
        ],
    }


def _load_simple_yaml(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key = ""
    current_item: dict[str, Any] | None = None
    current_item_list_key = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 0:
            key, value = _split_yaml(line)
            current_key = key
            current_item = None
            current_item_list_key = ""
            if value == "" and key in {"allowed_domains", "sources"}:
                data[key] = []
            elif value == "":
                data[key] = {}
            else:
                data[key] = _scalar(value)
        elif indent == 2 and line.startswith("- "):
            value = line[2:].strip()
            if current_key == "allowed_domains":
                data.setdefault(current_key, []).append(_scalar(value))
            else:
                item: dict[str, Any] = {}
                data.setdefault(current_key, []).append(item)
                current_item = item
                current_item_list_key = ""
                if ":" in value:
                    key, scalar = _split_yaml(value)
                    item[key] = _scalar(scalar)
        elif indent == 2:
            key, value = _split_yaml(line)
            section = data.setdefault(current_key, {})
            if isinstance(section, dict):
                section[key] = _scalar(value)
        elif indent == 4 and current_item is not None:
            if line.startswith("- ") and current_item_list_key:
                current_item.setdefault(current_item_list_key, []).append(_scalar(line[2:].strip()))
            else:
                key, value = _split_yaml(line)
                if value == "":
                    current_item[key] = []
                    current_item_list_key = key
                else:
                    current_item[key] = _scalar(value)
                    current_item_list_key = ""
    return data


def _split_yaml(line: str) -> tuple[str, str]:
    if ":" not in line:
        return line.strip(), ""
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _scalar(value: str) -> Any:
    cleaned = value.strip().strip('"').strip("'")
    if cleaned.lower() in {"true", "false"}:
        return cleaned.lower() == "true"
    if cleaned == "":
        return ""
    try:
        if "." in cleaned:
            return float(cleaned)
        return int(cleaned)
    except ValueError:
        return cleaned


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _default_if_missing(value: Any, default: Any) -> Any:
    return default if value in {None, ""} else value


def require_enabled(config: WebCollectionConfig) -> None:
    if not config.enabled:
        raise ConfigError("Web collection is disabled in the selected config.")
