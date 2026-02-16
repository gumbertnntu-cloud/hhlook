from __future__ import annotations

from datetime import datetime

from hh_monitor.models import Vacancy
from hh_monitor.storage import get_vacancies_by_ids, init_db, upsert_vacancy


def test_get_vacancies_by_ids_preserves_requested_order(tmp_path) -> None:
    db_path = tmp_path / "hh_monitor.db"
    init_db(db_path)

    now = datetime(2026, 2, 15, 12, 0, 0)
    v1 = Vacancy(
        vacancy_id="v1",
        url="https://hh.ru/vacancy/v1",
        title="Chief of Staff",
        company="ACME",
        snippet="strategy",
        normalized_hash="h1",
        published_at=now,
    )
    v2 = Vacancy(
        vacancy_id="v2",
        url="https://hh.ru/vacancy/v2",
        title="COO",
        company="Beta",
        snippet="operations",
        normalized_hash="h2",
        published_at=now,
    )

    upsert_vacancy(db_path=db_path, vacancy=v1, seen_at=now)
    upsert_vacancy(db_path=db_path, vacancy=v2, seen_at=now)

    rows = get_vacancies_by_ids(db_path, ["v2", "v1", "missing"])
    assert [row.vacancy_id for row in rows] == ["v2", "v1"]
    assert rows[0].title == "COO"
    assert rows[1].title == "Chief of Staff"
