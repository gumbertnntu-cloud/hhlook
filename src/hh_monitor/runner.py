from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .auth import validate_state
from .crawler import CrawlStats, crawl_vacancies, crawl_vacancy_details
from .filters import filter_vacancy
from .models import ChangeRow, RunParams, RunStats, SeenVacancyRow, Vacancy
from .report import generate_html_report
from .storage import (
    begin_run,
    finish_run,
    get_active_hashes,
    get_run_change_rows,
    get_run_seen_rows,
    get_vacancies_by_ids,
    init_db,
    insert_change,
    insert_run_item,
    mark_removed,
    upsert_vacancy,
)
from .utils import hash_normalized


@dataclass(slots=True)
class RunResult:
    run_id: int
    rows: list[ChangeRow]
    seen_rows: list[SeenVacancyRow]
    stats: RunStats
    html_report_path: Path
    processed_vacancy_ids: list[str]


class SessionInvalidError(RuntimeError):
    pass


def _compute_hash(vacancy: Vacancy) -> str:
    return hash_normalized(
        [
            vacancy.title,
            vacancy.company,
            vacancy.salary_raw,
            vacancy.area,
            vacancy.snippet,
            vacancy.description,
        ]
    )


def _merge_stats(crawl_stats: CrawlStats, run_stats: RunStats) -> RunStats:
    run_stats.pages_processed = crawl_stats.pages_processed
    run_stats.cards_seen = crawl_stats.cards_seen
    run_stats.cards_after_date_filter = crawl_stats.cards_after_date_filter
    run_stats.deep_opened = crawl_stats.deep_opened
    run_stats.errors.extend(crawl_stats.errors)
    return run_stats


def run_pipeline(
    *,
    project_root: Path,
    settings: object,
    logger: logging.Logger,
    deep_target_ids: list[str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> RunResult:
    db_path = project_root / settings.paths.db_path
    state_path = project_root / settings.paths.state_path
    report_path = project_root / settings.paths.reports_dir / "latest.html"

    init_db(db_path)

    if not validate_state(settings.search.base_url, state_path, logger):
        raise SessionInvalidError("Session state is missing or expired. Run authorization first.")

    params = RunParams(
        mode=settings.search.mode,
        max_pages=settings.search.max_pages,
        max_age_days=settings.search.max_age_days,
        query_text=settings.search.query_text,
    )
    cutoff_dt = datetime.now() - timedelta(days=settings.search.max_age_days)
    run_id = begin_run(db_path, params, cutoff_dt)
    if progress_callback:
        progress_callback(f"__RUN_ID__:{run_id}")

    stats = RunStats()
    try:
        deep_targets = deep_target_ids or []
        if settings.search.mode == "deep" and deep_targets:
            queued_vacancies = get_vacancies_by_ids(db_path, deep_targets)
            vacancies, crawl_stats = crawl_vacancy_details(
                vacancies=queued_vacancies,
                state_path=state_path,
                runtime=settings.runtime,
                logger=logger,
                progress_callback=progress_callback,
            )
        else:
            vacancies, crawl_stats = crawl_vacancies(
                base_url=settings.search.base_url,
                area=settings.search.area,
                query_text=settings.search.query_text,
                mode=settings.search.mode,
                max_pages=settings.search.max_pages,
                cutoff_dt=cutoff_dt,
                state_path=state_path,
                runtime=settings.runtime,
                logger=logger,
                progress_callback=progress_callback,
            )
        _merge_stats(crawl_stats, stats)

        deduped: dict[str, Vacancy] = {vacancy.vacancy_id: vacancy for vacancy in vacancies}
        previous_active = get_active_hashes(db_path)
        seen_ids: set[str] = set()
        now = datetime.utcnow()

        for vacancy in deduped.values():
            if settings.search.mode == "deep" and deep_targets:
                matched, reason = True, "deep:queue"
            else:
                include_description = settings.search.mode == "deep"
                matched, reason = filter_vacancy(
                    vacancy=vacancy,
                    include_keywords=settings.filters.include_keywords,
                    exclude_keywords=settings.filters.exclude_keywords,
                    min_salary=settings.filters.min_salary,
                    include_description=include_description,
                )
            if not matched:
                continue

            vacancy.match_reason = reason
            vacancy.normalized_hash = _compute_hash(vacancy)
            seen_ids.add(vacancy.vacancy_id)

            old_hash = previous_active.get(vacancy.vacancy_id)
            status = "seen"
            if old_hash is None:
                status = "new"
                stats.new_count += 1
                insert_change(
                    db_path=db_path,
                    run_id=run_id,
                    vacancy_id=vacancy.vacancy_id,
                    change_type="new",
                    old_hash=None,
                    new_hash=vacancy.normalized_hash,
                    changed_at=now,
                )
            elif old_hash != vacancy.normalized_hash:
                status = "updated"
                stats.updated_count += 1
                insert_change(
                    db_path=db_path,
                    run_id=run_id,
                    vacancy_id=vacancy.vacancy_id,
                    change_type="updated",
                    old_hash=old_hash,
                    new_hash=vacancy.normalized_hash,
                    changed_at=now,
                )

            upsert_vacancy(db_path=db_path, vacancy=vacancy, seen_at=now)
            insert_run_item(
                db_path=db_path,
                run_id=run_id,
                vacancy_id=vacancy.vacancy_id,
                seen_at=now,
                match_reason=reason,
                parse_status=status,
            )

        stats.cards_after_keyword_filter = len(seen_ids)
        if not (settings.search.mode == "deep" and deep_targets):
            removed_ids = sorted(set(previous_active.keys()) - seen_ids)
            if removed_ids:
                mark_removed(db_path, removed_ids)
                for vacancy_id in removed_ids:
                    stats.removed_count += 1
                    insert_change(
                        db_path=db_path,
                        run_id=run_id,
                        vacancy_id=vacancy_id,
                        change_type="removed",
                        old_hash=previous_active.get(vacancy_id),
                        new_hash=None,
                        changed_at=now,
                    )

        finish_run(db_path=db_path, run_id=run_id, status="ok", stats=asdict(stats))
    except Exception:
        finish_run(db_path=db_path, run_id=run_id, status="failed", stats=asdict(stats))
        raise

    rows = get_run_change_rows(db_path, run_id)
    seen_rows = get_run_seen_rows(db_path, run_id)
    html_report_path = generate_html_report(rows=rows, output_path=report_path, run_id=run_id)
    return RunResult(
        run_id=run_id,
        rows=rows,
        seen_rows=seen_rows,
        stats=stats,
        html_report_path=html_report_path,
        processed_vacancy_ids=sorted(seen_ids),
    )
