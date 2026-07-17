"""TradingView daily movers provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from intraday_scanner.providers.daily_movers_base import (
    CURRENT_WEB_KIND,
    CURRENT_WEB_ROLE,
    normalize_daily_mover_rows,
    retain_file_artifact,
)
from intraday_scanner.providers.public_table_provider import extract_html_tables, select_best_table
from intraday_scanner.providers.web_source_base import (
    WebCollectionConfig,
    WebSourceConfig,
    fetch_text,
)


class TradingViewDailyMoversProvider:
    def __init__(self, source: WebSourceConfig, config: WebCollectionConfig):
        self.source = source
        self.config = config

    def collect(self, *, market_date: str, out_dir: str | Path) -> dict[str, Any]:
        output_dir = Path(out_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        fetch = fetch_text(self.source, self.config)
        if fetch.status != "success":
            return {
                "status": "failed",
                "source": self.source.name,
                "source_type": self.source.type,
                "failure_reason": fetch.failure_reason,
                "rows": [],
                "rejected_rows": [],
            }
        artifact_path: Path | None = None
        artifact_ref = ""
        if self.config.save_raw:
            debug_path = output_dir / "tradingview_daily_movers.html"
            debug_path.write_text(
                fetch.content,
                encoding="utf-8",
            )
            artifact_ref, artifact_path = retain_file_artifact(
                debug_path,
                artifact_dir=output_dir / "source_artifacts",
            )
        tables = extract_html_tables(fetch.content)
        best = select_best_table(tables)
        if best is None:
            return {
                "status": "failed",
                "source": self.source.name,
                "source_type": self.source.type,
                "failure_reason": "no mover table found",
                "rows": [],
                "rejected_rows": [],
            }
        rows, rejected, rejection_counts = normalize_daily_mover_rows(
            best.rows,
            market_date=market_date,
            source=self.source.name or "tradingview_daily_movers",
            source_url=fetch.url,
            source_confidence=55.0,
            data_quality="Unverified public/free web shadow data",
            extracted_at=fetch.completed_at,
            dataset_role=CURRENT_WEB_ROLE,
            prospective_signal_eligible=False,
            source_snapshot_kind=CURRENT_WEB_KIND,
            ingestion_channel="public_web_current_session_gainers",
            source_artifact_ref=artifact_ref,
            source_artifact_path=(
                str(artifact_path.resolve()) if artifact_path is not None else ""
            ),
            source_coverage_complete=False,
            list_coverage_complete=False,
            expected_row_count=None,
            corporate_action_status="unverified",
            eod_label_eligible=False,
        )
        return {
            "status": "success" if rows else "no_valid_rows",
            "source": self.source.name,
            "source_type": self.source.type,
            "source_url": fetch.url,
            "source_artifact_ref": artifact_ref,
            "source_artifact_path": (
                str(artifact_path.resolve()) if artifact_path is not None else ""
            ),
            "rows": rows,
            "rejected_rows": rejected,
            "rejection_reason_counts": rejection_counts,
            "rows_extracted": len(best.rows),
            "rows_normalized": len(rows),
            "rows_rejected": len(rejected),
            "table_count": len(tables),
            "selected_table_index": best.index,
        }
