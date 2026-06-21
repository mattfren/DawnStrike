"""Configuration loading for local and provider-backed scanner runs."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

from intraday_scanner.errors import ConfigError


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"Invalid .env line {line_number}: expected KEY=value")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env(name: str, default: str, env_values: dict[str, str]) -> str:
    return os.environ.get(name, env_values.get(name, default))


def _env_any(names: list[str], default: str, env_values: dict[str, str]) -> str:
    for name in names:
        value = os.environ.get(name, env_values.get(name))
        if value not in {None, ""}:
            return str(value)
    return default


def _to_float(name: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def _to_int(name: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _to_bool(value: str) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


@dataclass(frozen=True)
class ScannerConfig:
    provider: str = "csv"
    output_dir: Path = Path("outputs/latest_scan")
    database_path: Path = Path("data/scanner.sqlite")
    signal_time: str = "09:30"
    premarket_start: str = "04:00"
    premarket_end: str = "09:29"
    lunch_exit_time: str = "12:30"
    close_exit_time: str = "15:59"
    slippage_bps: float = 50.0
    entry_mode: str = "open"
    min_gap_pct: float = 15.0
    min_premarket_dollar_volume: float = 500_000.0
    min_premarket_share_volume: int = 100_000
    min_price: float = 0.50
    max_price: float = 25.0
    top_n: int = 10
    wide_spread_pct: float = 5.0
    monitor_drop_from_watch_pct: float = 8.0
    monitor_volume_collapse_ratio: float = 0.45
    monitor_rejection_range_pct: float = 65.0
    ideal_gap_low_pct: float = 35.0
    ideal_gap_high_pct: float = 140.0
    max_credible_gap_pct: float = 300.0
    explosive_score_threshold: float = 70.0
    explosive_top_n: int = 3
    score_weight_gap: float = 1.0
    score_weight_liquidity: float = 1.0
    score_weight_float_rotation: float = 1.0
    score_weight_range: float = 1.0
    score_weight_catalyst: float = 1.0
    score_weight_execution: float = 1.0
    score_weight_data_quality: float = 1.0
    score_weight_risk_penalty: float = 1.0
    openai_api_key: str = ""
    alpaca_api_key_id: str = ""
    alpaca_api_secret_key: str = ""
    alpaca_data_feed: str = "iex"
    polygon_api_key: str = ""
    databento_api_key: str = ""
    news_api_key: str = ""
    benzinga_api_key: str = ""
    finnhub_api_key: str = ""
    database_url: str = ""
    request_timeout_seconds: float = 15.0
    request_retries: int = 3
    notifier_channels: str = "console"
    alert_score_threshold: float = 82.0
    email_smtp_host: str = ""
    email_smtp_port: int = 587
    email_username: str = ""
    email_password: str = ""
    email_from: str = ""
    email_to: str = ""
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_message_style: str = "compact"
    telegram_max_morning_chars: int = 1200
    telegram_max_alert_chars: int = 600
    telegram_max_summary_chars: int = 900
    telegram_include_debug_fields: bool = False
    telegram_send_summary_on_no_data: bool = False
    telegram_send_outcome_reminder_on_no_picks: bool = False

    def validate(self) -> None:
        if self.provider not in {"csv", "alpaca"}:
            raise ConfigError("provider must be one of: csv, alpaca")
        if self.min_price < 0:
            raise ConfigError("min_price must be non-negative")
        if self.max_price <= self.min_price:
            raise ConfigError("max_price must be greater than min_price")
        if self.top_n <= 0:
            raise ConfigError("top_n must be positive")
        if self.explosive_top_n <= 0:
            raise ConfigError("explosive_top_n must be positive")
        if self.slippage_bps < 0:
            raise ConfigError("slippage_bps must be non-negative")
        if self.entry_mode not in {"open", "breakout"}:
            raise ConfigError("entry_mode must be one of: open, breakout")
        if self.ideal_gap_low_pct <= self.min_gap_pct:
            raise ConfigError("ideal_gap_low_pct must be greater than min_gap_pct")
        if self.ideal_gap_high_pct <= self.ideal_gap_low_pct:
            raise ConfigError("ideal_gap_high_pct must be greater than ideal_gap_low_pct")
        if self.max_credible_gap_pct <= self.ideal_gap_high_pct:
            raise ConfigError("max_credible_gap_pct must be greater than ideal_gap_high_pct")
        if self.request_timeout_seconds <= 0:
            raise ConfigError("request_timeout_seconds must be positive")
        if self.request_retries < 1:
            raise ConfigError("request_retries must be at least 1")
        if self.alert_score_threshold < 0:
            raise ConfigError("alert_score_threshold must be non-negative")
        if self.monitor_drop_from_watch_pct < 0:
            raise ConfigError("monitor_drop_from_watch_pct must be non-negative")
        if not 0 < self.monitor_volume_collapse_ratio <= 1:
            raise ConfigError("monitor_volume_collapse_ratio must be between 0 and 1")
        if not 0 <= self.monitor_rejection_range_pct <= 100:
            raise ConfigError("monitor_rejection_range_pct must be between 0 and 100")
        if self.email_smtp_port <= 0:
            raise ConfigError("email_smtp_port must be positive")
        if self.telegram_message_style not in {"compact", "legacy"}:
            raise ConfigError("telegram_message_style must be one of: compact, legacy")
        if self.telegram_max_morning_chars <= 0:
            raise ConfigError("telegram_max_morning_chars must be positive")
        if self.telegram_max_alert_chars <= 0:
            raise ConfigError("telegram_max_alert_chars must be positive")
        if self.telegram_max_summary_chars <= 0:
            raise ConfigError("telegram_max_summary_chars must be positive")
        weights = [
            self.score_weight_gap,
            self.score_weight_liquidity,
            self.score_weight_float_rotation,
            self.score_weight_range,
            self.score_weight_catalyst,
            self.score_weight_execution,
            self.score_weight_data_quality,
            self.score_weight_risk_penalty,
        ]
        if any(weight <= 0 for weight in weights):
            raise ConfigError("score weights must be positive")

    def with_overrides(self, **overrides: Any) -> ScannerConfig:
        clean = {key: value for key, value in overrides.items() if value is not None}
        allowed = {field.name for field in fields(self)}
        unknown = sorted(set(clean) - allowed)
        if unknown:
            raise ConfigError(f"Unknown config override(s): {', '.join(unknown)}")
        updated = replace(self, **clean)
        updated.validate()
        return updated

    def public_dict(self) -> dict[str, Any]:
        secret_fields = {
            "openai_api_key",
            "alpaca_api_key_id",
            "alpaca_api_secret_key",
            "polygon_api_key",
            "databento_api_key",
            "news_api_key",
            "benzinga_api_key",
            "finnhub_api_key",
            "database_url",
            "email_password",
            "discord_webhook_url",
            "telegram_bot_token",
            "telegram_chat_id",
        }
        data = {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name not in secret_fields
        }
        data["output_dir"] = str(self.output_dir)
        data["database_path"] = str(self.database_path)
        return data


def load_config(env_file: str | Path = ".env", **overrides: Any) -> ScannerConfig:
    env_values = _parse_env_file(Path(env_file))
    config = ScannerConfig(
        provider=_env("INTRADAY_PROVIDER", "csv", env_values).lower(),
        output_dir=Path(_env("INTRADAY_OUTPUT_DIR", "outputs/latest_scan", env_values)),
        database_path=Path(_env("INTRADAY_DATABASE_PATH", "data/scanner.sqlite", env_values)),
        signal_time=_env("INTRADAY_SIGNAL_TIME", "09:30", env_values),
        premarket_start=_env("INTRADAY_PREMARKET_START", "04:00", env_values),
        premarket_end=_env("INTRADAY_PREMARKET_END", "09:29", env_values),
        lunch_exit_time=_env("INTRADAY_LUNCH_EXIT_TIME", "12:30", env_values),
        close_exit_time=_env("INTRADAY_CLOSE_EXIT_TIME", "15:59", env_values),
        slippage_bps=_to_float(
            "INTRADAY_SLIPPAGE_BPS", _env("INTRADAY_SLIPPAGE_BPS", "50", env_values)
        ),
        entry_mode=_env("INTRADAY_ENTRY_MODE", "open", env_values),
        min_gap_pct=_to_float(
            "INTRADAY_MIN_GAP_PCT", _env("INTRADAY_MIN_GAP_PCT", "15", env_values)
        ),
        min_premarket_dollar_volume=_to_float(
            "INTRADAY_MIN_PREMARKET_DOLLAR_VOLUME",
            _env("INTRADAY_MIN_PREMARKET_DOLLAR_VOLUME", "500000", env_values),
        ),
        min_premarket_share_volume=_to_int(
            "INTRADAY_MIN_PREMARKET_SHARE_VOLUME",
            _env("INTRADAY_MIN_PREMARKET_SHARE_VOLUME", "100000", env_values),
        ),
        min_price=_to_float("INTRADAY_MIN_PRICE", _env("INTRADAY_MIN_PRICE", "0.5", env_values)),
        max_price=_to_float("INTRADAY_MAX_PRICE", _env("INTRADAY_MAX_PRICE", "25", env_values)),
        top_n=_to_int("INTRADAY_TOP_N", _env("INTRADAY_TOP_N", "10", env_values)),
        wide_spread_pct=_to_float(
            "INTRADAY_WIDE_SPREAD_PCT", _env("INTRADAY_WIDE_SPREAD_PCT", "5", env_values)
        ),
        monitor_drop_from_watch_pct=_to_float(
            "INTRADAY_MONITOR_DROP_FROM_WATCH_PCT",
            _env("INTRADAY_MONITOR_DROP_FROM_WATCH_PCT", "8", env_values),
        ),
        monitor_volume_collapse_ratio=_to_float(
            "INTRADAY_MONITOR_VOLUME_COLLAPSE_RATIO",
            _env("INTRADAY_MONITOR_VOLUME_COLLAPSE_RATIO", "0.45", env_values),
        ),
        monitor_rejection_range_pct=_to_float(
            "INTRADAY_MONITOR_REJECTION_RANGE_PCT",
            _env("INTRADAY_MONITOR_REJECTION_RANGE_PCT", "65", env_values),
        ),
        ideal_gap_low_pct=_to_float(
            "INTRADAY_IDEAL_GAP_LOW_PCT",
            _env("INTRADAY_IDEAL_GAP_LOW_PCT", "35", env_values),
        ),
        ideal_gap_high_pct=_to_float(
            "INTRADAY_IDEAL_GAP_HIGH_PCT",
            _env("INTRADAY_IDEAL_GAP_HIGH_PCT", "140", env_values),
        ),
        max_credible_gap_pct=_to_float(
            "INTRADAY_MAX_CREDIBLE_GAP_PCT",
            _env("INTRADAY_MAX_CREDIBLE_GAP_PCT", "300", env_values),
        ),
        explosive_score_threshold=_to_float(
            "INTRADAY_EXPLOSIVE_SCORE_THRESHOLD",
            _env("INTRADAY_EXPLOSIVE_SCORE_THRESHOLD", "70", env_values),
        ),
        explosive_top_n=_to_int(
            "INTRADAY_EXPLOSIVE_TOP_N", _env("INTRADAY_EXPLOSIVE_TOP_N", "3", env_values)
        ),
        score_weight_gap=_to_float(
            "INTRADAY_SCORE_WEIGHT_GAP",
            _env("INTRADAY_SCORE_WEIGHT_GAP", "1.0", env_values),
        ),
        score_weight_liquidity=_to_float(
            "INTRADAY_SCORE_WEIGHT_LIQUIDITY",
            _env("INTRADAY_SCORE_WEIGHT_LIQUIDITY", "1.0", env_values),
        ),
        score_weight_float_rotation=_to_float(
            "INTRADAY_SCORE_WEIGHT_FLOAT_ROTATION",
            _env("INTRADAY_SCORE_WEIGHT_FLOAT_ROTATION", "1.0", env_values),
        ),
        score_weight_range=_to_float(
            "INTRADAY_SCORE_WEIGHT_RANGE",
            _env("INTRADAY_SCORE_WEIGHT_RANGE", "1.0", env_values),
        ),
        score_weight_catalyst=_to_float(
            "INTRADAY_SCORE_WEIGHT_CATALYST",
            _env("INTRADAY_SCORE_WEIGHT_CATALYST", "1.0", env_values),
        ),
        score_weight_execution=_to_float(
            "INTRADAY_SCORE_WEIGHT_EXECUTION",
            _env("INTRADAY_SCORE_WEIGHT_EXECUTION", "1.0", env_values),
        ),
        score_weight_data_quality=_to_float(
            "INTRADAY_SCORE_WEIGHT_DATA_QUALITY",
            _env("INTRADAY_SCORE_WEIGHT_DATA_QUALITY", "1.0", env_values),
        ),
        score_weight_risk_penalty=_to_float(
            "INTRADAY_SCORE_WEIGHT_RISK_PENALTY",
            _env("INTRADAY_SCORE_WEIGHT_RISK_PENALTY", "1.0", env_values),
        ),
        openai_api_key=_env("OPENAI_API_KEY", "", env_values),
        alpaca_api_key_id=_env("ALPACA_API_KEY_ID", "", env_values),
        alpaca_api_secret_key=_env("ALPACA_API_SECRET_KEY", "", env_values),
        alpaca_data_feed=_env("ALPACA_DATA_FEED", "iex", env_values),
        polygon_api_key=_env("POLYGON_API_KEY", "", env_values),
        databento_api_key=_env("DATABENTO_API_KEY", "", env_values),
        news_api_key=_env("NEWS_API_KEY", "", env_values),
        benzinga_api_key=_env("BENZINGA_API_KEY", "", env_values),
        finnhub_api_key=_env("FINNHUB_API_KEY", "", env_values),
        database_url=_env("DATABASE_URL", "", env_values),
        request_timeout_seconds=_to_float(
            "INTRADAY_REQUEST_TIMEOUT_SECONDS",
            _env("INTRADAY_REQUEST_TIMEOUT_SECONDS", "15", env_values),
        ),
        request_retries=_to_int(
            "INTRADAY_REQUEST_RETRIES", _env("INTRADAY_REQUEST_RETRIES", "3", env_values)
        ),
        notifier_channels=_env("INTRADAY_NOTIFIER_CHANNELS", "console", env_values),
        alert_score_threshold=_to_float(
            "INTRADAY_ALERT_SCORE_THRESHOLD",
            _env("INTRADAY_ALERT_SCORE_THRESHOLD", "82", env_values),
        ),
        email_smtp_host=_env_any(["SMTP_HOST", "INTRADAY_EMAIL_SMTP_HOST"], "", env_values),
        email_smtp_port=_to_int(
            "SMTP_PORT", _env_any(["SMTP_PORT", "INTRADAY_EMAIL_SMTP_PORT"], "587", env_values)
        ),
        email_username=_env_any(["SMTP_USER", "INTRADAY_EMAIL_USERNAME"], "", env_values),
        email_password=_env_any(["SMTP_PASSWORD", "INTRADAY_EMAIL_PASSWORD"], "", env_values),
        email_from=_env("INTRADAY_EMAIL_FROM", "", env_values),
        email_to=_env("INTRADAY_EMAIL_TO", "", env_values),
        discord_webhook_url=_env_any(
            ["DISCORD_WEBHOOK_URL", "INTRADAY_DISCORD_WEBHOOK_URL"], "", env_values
        ),
        telegram_bot_token=_env_any(
            ["TELEGRAM_BOT_TOKEN", "INTRADAY_TELEGRAM_BOT_TOKEN"], "", env_values
        ),
        telegram_chat_id=_env_any(
            ["TELEGRAM_CHAT_ID", "INTRADAY_TELEGRAM_CHAT_ID"], "", env_values
        ),
        telegram_message_style=_env(
            "INTRADAY_TELEGRAM_MESSAGE_STYLE", "compact", env_values
        ).lower(),
        telegram_max_morning_chars=_to_int(
            "INTRADAY_TELEGRAM_MAX_MORNING_CHARS",
            _env("INTRADAY_TELEGRAM_MAX_MORNING_CHARS", "1200", env_values),
        ),
        telegram_max_alert_chars=_to_int(
            "INTRADAY_TELEGRAM_MAX_ALERT_CHARS",
            _env("INTRADAY_TELEGRAM_MAX_ALERT_CHARS", "600", env_values),
        ),
        telegram_max_summary_chars=_to_int(
            "INTRADAY_TELEGRAM_MAX_SUMMARY_CHARS",
            _env("INTRADAY_TELEGRAM_MAX_SUMMARY_CHARS", "900", env_values),
        ),
        telegram_include_debug_fields=_to_bool(
            _env("INTRADAY_TELEGRAM_INCLUDE_DEBUG_FIELDS", "false", env_values)
        ),
        telegram_send_summary_on_no_data=_to_bool(
            _env("INTRADAY_TELEGRAM_SEND_SUMMARY_ON_NO_DATA", "false", env_values)
        ),
        telegram_send_outcome_reminder_on_no_picks=_to_bool(
            _env("INTRADAY_TELEGRAM_SEND_OUTCOME_REMINDER_ON_NO_PICKS", "false", env_values)
        ),
    ).with_overrides(**overrides)
    config.validate()
    return config
