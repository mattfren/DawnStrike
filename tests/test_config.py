from intraday_scanner.config import load_config


def test_config_accepts_requested_secret_aliases(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=openai-secret",
                "POLYGON_API_KEY=polygon-secret",
                "DATABENTO_API_KEY=databento-secret",
                "NEWS_API_KEY=news-secret",
                "BENZINGA_API_KEY=benzinga-secret",
                "FINNHUB_API_KEY=finnhub-secret",
                "DISCORD_WEBHOOK_URL=https://discord.example",
                "TELEGRAM_BOT_TOKEN=telegram-secret",
                "TELEGRAM_CHAT_ID=123",
                "SMTP_HOST=smtp.example",
                "SMTP_PORT=2525",
                "SMTP_USER=user",
                "SMTP_PASSWORD=password",
                "DATABASE_URL=sqlite:///prod.sqlite",
                "INTRADAY_MONITOR_DROP_FROM_WATCH_PCT=6",
                "INTRADAY_MONITOR_VOLUME_COLLAPSE_RATIO=0.4",
                "INTRADAY_MONITOR_REJECTION_RANGE_PCT=70",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    config = load_config(env_file)

    assert config.openai_api_key == "openai-secret"
    assert config.discord_webhook_url == "https://discord.example"
    assert config.email_smtp_host == "smtp.example"
    assert config.email_smtp_port == 2525
    assert config.database_url == "sqlite:///prod.sqlite"
    assert config.monitor_drop_from_watch_pct == 6
    assert config.monitor_volume_collapse_ratio == 0.4
    assert config.monitor_rejection_range_pct == 70
    public = config.public_dict()
    assert "openai_api_key" not in public
    assert "discord_webhook_url" not in public
