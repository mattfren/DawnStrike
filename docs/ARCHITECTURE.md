# Architecture

Dawnstrike is split into thin adapters, business services, and durable outputs.

## Components

- `config.py`: loads defaults, `.env`, and CLI overrides.
- `models.py`: canonical snapshot and scored-candidate schemas.
- `providers/`: market, news, and SEC/filing adapters. `CsvSnapshotProvider` is offline-first; `AlpacaProvider` is live market data only.
- `scoring.py`: deterministic scoring and avoid-list logic.
- `formula.py`: versioned Dawnstrike equation and derived signal factors.
- `expectancy.py`: conservative expected paper-return model with sparse-sample confidence caps.
- `services/scan_service.py`: provider-to-score orchestration.
- `services/audit_service.py`: paper audit calculations for ranked candidates.
- `services/alert_service.py`: monitor-row, headline, and filing alert triggers plus alert dedupe payloads.
- `services/provider_health_service.py`: sanitized provider readiness records for the dashboard.
- `services/performance_service.py`: cumulative paper-audit performance reporting.
- `services/recommendation_service.py`: timestamped recommendation payload construction.
- `scheduler.py`: local Windows-friendly schedule plan.
- `storage/sqlite_store.py`: local durable storage with tables shaped for a future Postgres adapter.
- `notifiers/` and `notifications/`: console, email, Discord webhook, and Telegram research-alert adapters.
- `ai/`: headline and thesis-monitor abstractions. The default classifier is deterministic/offline.
- `dashboard/`: Streamlit data loading and UI helpers.
- `reporting.py`: CSV/JSON output files for dashboard and review.

## Data Flow

1. Provider returns canonical `SnapshotRow` objects.
2. Scoring produces all candidates, ranked candidates, top explosive names, and avoid list.
3. Reporting writes durable CSV/JSON outputs.
4. Optional SQLite persistence stores scan runs, snapshots, recommendation theses, monitor checks/events, alerts, paper audits, performance rows, provider health, and notification state.
5. Notification dispatch reads persisted scans or audit summaries and dedupes by event key.
6. Monitor checks can emit deduped alert events when invalidation, fade, extension, halt, offering, spread-risk, negative headline, or SEC dilution-risk conditions appear.
7. Dashboard reads sample CSV, latest output files, or SQLite history.

## Extension Points

Add another data provider by implementing `MarketDataProvider` in `providers/base.py`. Add news or filing feeds with `NewsProvider` and `SECProvider`. Polygon, Databento, Benzinga, Finnhub, NewsAPI, or other providers can plug in at those boundaries. Add another database by implementing the `ScanStore` protocol in `storage/base.py`. Add another notifier by implementing `BaseNotifier`.

No auto-trading component exists in this architecture.
