"""Safe public web source helpers for Dawnstrike research collection."""

from __future__ import annotations

import hashlib
import json
import re
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
from intraday_scanner.network_safety import open_allowlisted_url

DEFAULT_WEB_CONFIG_PATH = Path("config/web_sources.yaml")


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
    production_contract: bool = False
    primary_quote_source: str = ""
    secondary_quote_source: str = ""
    primary_benchmark: str = ""
    secondary_benchmark: str = ""
    universe_version: str = ""


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
    path = Path(_portable_config_path(config_path)) if config_path else DEFAULT_WEB_CONFIG_PATH
    if not path.is_file():
        raise ConfigError(
            "Web source configuration is required and was not found: "
            f"{path}. The example configuration is development-only and is never "
            "a production fallback."
        )
    return _web_collection_config(_load_simple_yaml(path))


def _web_collection_config(data: dict[str, Any]) -> WebCollectionConfig:
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
                path=_portable_config_path(row.get("path")),
                fixture_path=_portable_config_path(row.get("fixture_path")),
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
        production_contract=_bool(data.get("production_contract", False)),
        primary_quote_source=str(data.get("primary_quote_source") or ""),
        secondary_quote_source=str(data.get("secondary_quote_source") or ""),
        primary_benchmark=str(data.get("primary_benchmark") or ""),
        secondary_benchmark=str(data.get("secondary_benchmark") or ""),
        universe_version=str(data.get("universe_version") or ""),
    )


def production_contract_status(config: WebCollectionConfig) -> dict[str, Any]:
    """Validate a declared production source contract without weakening fixtures."""

    if not config.production_contract:
        forward_violations: list[str] = []
        if not config.enabled:
            forward_violations.append("web_collection_disabled")
        normalized_agent = config.user_agent.strip().lower()
        contact_match = re.search(
            r"contact:\s*([^\s@]+@[^\s@]+\.[^\s@]+)",
            normalized_agent,
        )
        if (
            not contact_match
            or "required_" in normalized_agent
            or "example.com" in normalized_agent
        ):
            forward_violations.append("accountable_user_agent_contact_required")
        candidate_sources = [
            source
            for source in config.sources
            if source.enabled
            and source.type
            in {
                "local_inbox",
                "public_table_url",
                "browser_table_url",
                "alpaca_screener_api",
            }
        ]
        invalid_candidates = [
            source.name or "unnamed"
            for source in candidate_sources
            if not _candidate_source_semantically_valid(source, config.allowed_domains)
        ]
        if not candidate_sources:
            forward_violations.append("enabled_candidate_source_required")
        for source_name in invalid_candidates:
            forward_violations.append(f"invalid_candidate_source:{source_name}")
        return {
            "status": "FORWARD_RESEARCH_ONLY",
            "ready": not forward_violations,
            "violations": forward_violations,
            "historical_backfill_enabled": False,
            "broker_execution_enabled": False,
        }
    violations: list[str] = []
    if not config.enabled:
        violations.append("web_collection_disabled")
    values = {
        "primary_quote_source": config.primary_quote_source,
        "secondary_quote_source": config.secondary_quote_source,
        "primary_benchmark": config.primary_benchmark,
        "secondary_benchmark": config.secondary_benchmark,
        "universe_version": config.universe_version,
    }
    for name, value in values.items():
        normalized = value.strip().upper()
        if not normalized or "REQUIRED" in normalized or "PLACEHOLDER" in normalized:
            violations.append(f"missing_{name}")
    user_agent = config.user_agent.upper()
    if (
        not re.search(r"CONTACT:\s*([^\s@]+@[^\s@]+\.[^\s@]+)", user_agent)
        or "YOUR_EMAIL_HERE" in user_agent
        or "REQUIRED_ACCOUNTABLE_EMAIL" in user_agent
        or "EXAMPLE.COM" in user_agent
    ):
        violations.append("accountable_user_agent_contact_required")
    enabled_by_name = {source.name: source for source in config.sources if source.enabled}
    required_safety_types = {
        "nasdaq_halts": "nasdaq_trade_halts_rss",
        "sec_edgar": "sec_edgar",
    }
    for source_name, source_type in required_safety_types.items():
        source = enabled_by_name.get(source_name)
        if source is None:
            violations.append(f"required_safety_source_disabled:{source_name}")
        elif source.type != source_type:
            violations.append(f"invalid_safety_source_type:{source_name}")
    candidate_sources = [
        source
        for source in config.sources
        if source.enabled
        and source.type
        in {
            "local_inbox",
            "public_table_url",
            "browser_table_url",
            "alpaca_screener_api",
        }
    ]
    if not candidate_sources:
        violations.append("enabled_candidate_source_required")
    for source in candidate_sources:
        if not _candidate_source_semantically_valid(source, config.allowed_domains):
            violations.append(f"invalid_candidate_source:{source.name or 'unnamed'}")
    if config.primary_benchmark not in {"SPY", "IWM"}:
        violations.append("primary_benchmark_must_be_spy_or_iwm")
    return {
        "status": "READY" if not violations else "BLOCKED_CONFIGURATION",
        "ready": not violations,
        "violations": violations,
        "primary_quote_source": config.primary_quote_source or None,
        "secondary_quote_source": config.secondary_quote_source or None,
        "primary_benchmark": config.primary_benchmark or None,
        "secondary_benchmark": config.secondary_benchmark or None,
        "universe_version": config.universe_version or None,
    }


def validate_web_source_config(config_path: str | Path) -> dict[str, Any]:
    """Parse and semantically validate the exact durable source configuration."""

    path = Path(config_path).resolve()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _invalid_web_source_config(path, exc)
    return validate_web_source_config_bytes(path, raw)


def validate_web_source_config_bytes(config_path: str | Path, raw: bytes) -> dict[str, Any]:
    """Validate semantics and digest from one caller-captured byte string."""

    path = Path(config_path).resolve()
    try:
        text = raw.decode("utf-8", "strict")
        config = _web_collection_config(_load_simple_yaml_text(text))
        contract = production_contract_status(config)
    except (ConfigError, OSError, TypeError, ValueError) as exc:
        return _invalid_web_source_config(path, exc)
    return {
        **contract,
        "config_path": str(path),
        "config_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _invalid_web_source_config(path: Path, exc: Exception) -> dict[str, Any]:
    return {
        "status": "BLOCKED_CONFIGURATION",
        "ready": False,
        "config_path": str(path),
        "config_sha256": None,
        "violations": ["source_config_unreadable_or_invalid"],
        "detail": str(exc),
    }


def _candidate_source_semantically_valid(
    source: WebSourceConfig,
    allowed_domains: tuple[str, ...],
) -> bool:
    if not source.name.strip():
        return False
    if source.type == "local_inbox":
        return bool(source.path.strip())
    if source.type == "alpaca_screener_api":
        return bool(source.name.strip())
    if source.type not in {"public_table_url", "browser_table_url"}:
        return False
    try:
        parsed = urllib.parse.urlparse(source.url)
    except ValueError:
        return False
    host = str(parsed.hostname or "").lower()
    allowed = tuple(domain.lower().lstrip(".") for domain in allowed_domains)
    return bool(
        parsed.scheme == "https"
        and host
        and any(host == domain or host.endswith("." + domain) for domain in allowed)
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
                    "text/html,application/xhtml+xml,application/xml,application/json,text/plain"
                ),
            },
        )
        with open_allowlisted_url(
            request,
            timeout=config.timeout_seconds,
            allowed_hosts=allowed_domains,
            allow_http=True,
        ) as response:
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
        "allowed_domains": [
            "nasdaqtrader.com",
            "sec.gov",
            "stockanalysis.com",
            "www.stockanalysis.com",
            "tradingview.com",
            "www.tradingview.com",
            "marketwatch.com",
            "www.marketwatch.com",
            "investing.com",
            "www.investing.com",
            "barchart.com",
            "www.barchart.com",
        ],
        "sources": [
            {
                "name": "local_inbox",
                "type": "local_inbox",
                "path": "data/inbox/screener",
                "enabled": True,
            },
            {
                "name": "stockanalysis_premarket",
                "type": "public_table_url",
                "url": "https://stockanalysis.com/markets/premarket/",
                "enabled": True,
            },
            {
                "name": "tradingview_premarket",
                "type": "public_table_url",
                "url": (
                    "https://www.tradingview.com/markets/stocks-usa/"
                    "market-movers-pre-market-gainers/"
                ),
                "enabled": True,
            },
            {
                "name": "marketwatch_movers",
                "type": "public_table_url",
                "url": "https://www.marketwatch.com/tools/us-market-movers",
                "enabled": False,
            },
            {
                "name": "investing_premarket",
                "type": "public_table_url",
                "url": "https://www.investing.com/equities/pre-market",
                "enabled": False,
            },
            {
                "name": "barchart_premarket_browser",
                "type": "browser_table_url",
                "url": "https://www.barchart.com/stocks/pre-market-trading",
                "enabled": False,
            },
            {"name": "nasdaq_symbols", "type": "nasdaq_symbol_directory", "enabled": True},
            {"name": "nasdaq_halts", "type": "nasdaq_trade_halts_rss", "enabled": False},
            {"name": "sec_edgar", "type": "sec_edgar", "enabled": False},
        ],
    }


def _load_simple_yaml(path: Path) -> dict[str, Any]:
    return _load_simple_yaml_text(path.read_text(encoding="utf-8"))


def _load_simple_yaml_text(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key = ""
    current_item: dict[str, Any] | None = None
    current_item_list_key = ""
    for raw_line in text.splitlines():
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


def _portable_config_path(value: str | Path | None) -> str:
    """Normalize file paths from hand-edited YAML before any OS sees them."""

    return str(value or "").replace("\\", "/")


def require_enabled(config: WebCollectionConfig) -> None:
    if not config.enabled:
        raise ConfigError("Web collection is disabled in the selected config.")
