from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import ChangeRow, RunParams, SeenVacancyRow, Vacancy

SQL_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS vacancies (
  vacancy_id TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  company TEXT NOT NULL,
  salary_raw TEXT,
  salary_from INTEGER,
  salary_to INTEGER,
  area TEXT,
  snippet TEXT,
  description TEXT,
  published_at TEXT,
  activity_text TEXT,
  date_unknown INTEGER NOT NULL DEFAULT 0,
  normalized_hash TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  mode TEXT NOT NULL,
  max_pages INTEGER NOT NULL,
  max_age_days INTEGER NOT NULL,
  cutoff_ts TEXT NOT NULL,
  query_text TEXT NOT NULL,
  status TEXT NOT NULL,
  stats_json TEXT
);

CREATE TABLE IF NOT EXISTS run_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  vacancy_id TEXT NOT NULL,
  seen_at TEXT NOT NULL,
  match_reason TEXT,
  parse_status TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  vacancy_id TEXT NOT NULL,
  change_type TEXT NOT NULL,
  old_hash TEXT,
  new_hash TEXT,
  changed_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SQL_CREATE_TABLES)
        conn.commit()


def begin_run(db_path: Path, params: RunParams, cutoff_ts: datetime) -> int:
    started_at = datetime.utcnow().isoformat()
    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO runs(
                started_at, mode, max_pages, max_age_days, cutoff_ts, query_text, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                params.mode,
                params.max_pages,
                params.max_age_days,
                cutoff_ts.isoformat(),
                params.query_text,
                "running",
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def finish_run(db_path: Path, run_id: int, status: str, stats: dict[str, object]) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE runs
            SET finished_at = ?, status = ?, stats_json = ?
            WHERE id = ?
            """,
            (datetime.utcnow().isoformat(), status, json.dumps(stats, ensure_ascii=False), run_id),
        )
        conn.commit()


def get_active_hashes(db_path: Path) -> dict[str, str]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT vacancy_id, normalized_hash FROM vacancies WHERE is_active = 1"
        ).fetchall()
    return {row["vacancy_id"]: row["normalized_hash"] for row in rows}


def upsert_vacancy(
    db_path: Path,
    vacancy: Vacancy,
    seen_at: datetime,
) -> None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT vacancy_id FROM vacancies WHERE vacancy_id = ?",
            (vacancy.vacancy_id,),
        ).fetchone()
        published_at = vacancy.published_at.isoformat() if vacancy.published_at else None
        payload = (
            vacancy.vacancy_id,
            vacancy.url,
            vacancy.title,
            vacancy.company,
            vacancy.salary_raw,
            vacancy.salary_from,
            vacancy.salary_to,
            vacancy.area,
            vacancy.snippet,
            vacancy.description,
            published_at,
            vacancy.activity_text,
            1 if vacancy.date_unknown else 0,
            vacancy.normalized_hash,
            seen_at.isoformat(),
        )
        if row is None:
            conn.execute(
                """
                INSERT INTO vacancies(
                  vacancy_id, url, title, company, salary_raw, salary_from, salary_to,
                  area, snippet, description, published_at, activity_text, date_unknown,
                  normalized_hash, first_seen_at, last_seen_at, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                payload + (seen_at.isoformat(),),
            )
        else:
            conn.execute(
                """
                UPDATE vacancies
                SET url = ?, title = ?, company = ?, salary_raw = ?, salary_from = ?,
                    salary_to = ?, area = ?, snippet = ?, description = ?, published_at = ?,
                    activity_text = ?, date_unknown = ?, normalized_hash = ?,
                    last_seen_at = ?, is_active = 1
                WHERE vacancy_id = ?
                """,
                (
                    vacancy.url,
                    vacancy.title,
                    vacancy.company,
                    vacancy.salary_raw,
                    vacancy.salary_from,
                    vacancy.salary_to,
                    vacancy.area,
                    vacancy.snippet,
                    vacancy.description,
                    published_at,
                    vacancy.activity_text,
                    1 if vacancy.date_unknown else 0,
                    vacancy.normalized_hash,
                    seen_at.isoformat(),
                    vacancy.vacancy_id,
                ),
            )
        conn.commit()


def insert_run_item(
    db_path: Path,
    run_id: int,
    vacancy_id: str,
    seen_at: datetime,
    match_reason: str,
    parse_status: str,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO run_items(run_id, vacancy_id, seen_at, match_reason, parse_status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, vacancy_id, seen_at.isoformat(), match_reason, parse_status),
        )
        conn.commit()


def insert_change(
    db_path: Path,
    run_id: int,
    vacancy_id: str,
    change_type: str,
    old_hash: str | None,
    new_hash: str | None,
    changed_at: datetime,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO changes(run_id, vacancy_id, change_type, old_hash, new_hash, changed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, vacancy_id, change_type, old_hash, new_hash, changed_at.isoformat()),
        )
        conn.commit()


def mark_removed(db_path: Path, vacancy_ids: list[str]) -> None:
    if not vacancy_ids:
        return
    placeholders = ",".join("?" for _ in vacancy_ids)
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE vacancies SET is_active = 0 WHERE vacancy_id IN ({placeholders})",
            vacancy_ids,
        )
        conn.commit()


def get_last_run_id(db_path: Path) -> int | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return int(row["id"]) if row else None


def get_run_change_rows(db_path: Path, run_id: int) -> list[ChangeRow]:
    query = """
    SELECT
      c.changed_at AS date_seen,
      c.vacancy_id,
      v.title,
      v.company,
      v.salary_raw AS salary,
      v.area,
      v.url,
      COALESCE(
        (SELECT ri.match_reason FROM run_items ri
         WHERE ri.run_id = c.run_id AND ri.vacancy_id = c.vacancy_id
         ORDER BY ri.id DESC LIMIT 1),
         ''
      ) AS match_reason,
      c.change_type AS status
    FROM changes c
    JOIN vacancies v ON v.vacancy_id = c.vacancy_id
    WHERE c.run_id = ?
    ORDER BY c.changed_at DESC
    """
    with _connect(db_path) as conn:
        rows = conn.execute(query, (run_id,)).fetchall()
    return [
        ChangeRow(
            date_seen=row["date_seen"],
            vacancy_id=row["vacancy_id"],
            title=row["title"],
            company=row["company"],
            salary=row["salary"] or "",
            area=row["area"] or "",
            url=row["url"] or "",
            match_reason=row["match_reason"] or "",
            status=row["status"],
        )
        for row in rows
    ]


def get_run_seen_rows(db_path: Path, run_id: int) -> list[SeenVacancyRow]:
    query = """
    SELECT
      ri.seen_at AS date_seen,
      ri.vacancy_id,
      v.title,
      v.company,
      v.salary_raw AS salary,
      v.area,
      v.url,
      ri.match_reason,
      ri.parse_status
    FROM run_items ri
    JOIN vacancies v ON v.vacancy_id = ri.vacancy_id
    WHERE ri.run_id = ?
    ORDER BY ri.id DESC
    """
    with _connect(db_path) as conn:
        rows = conn.execute(query, (run_id,)).fetchall()
    return [
        SeenVacancyRow(
            date_seen=row["date_seen"],
            vacancy_id=row["vacancy_id"],
            title=row["title"] or "",
            company=row["company"] or "",
            salary=row["salary"] or "",
            area=row["area"] or "",
            url=row["url"] or "",
            match_reason=row["match_reason"] or "",
            parse_status=row["parse_status"] or "seen",
        )
        for row in rows
    ]


def get_vacancies_by_ids(db_path: Path, vacancy_ids: list[str]) -> list[Vacancy]:
    if not vacancy_ids:
        return []
    placeholders = ",".join("?" for _ in vacancy_ids)
    query = f"""
    SELECT
      vacancy_id,
      url,
      title,
      company,
      salary_raw,
      salary_from,
      salary_to,
      area,
      snippet,
      description,
      activity_text,
      published_at,
      date_unknown
    FROM vacancies
    WHERE vacancy_id IN ({placeholders})
    """
    with _connect(db_path) as conn:
        rows = conn.execute(query, vacancy_ids).fetchall()

    by_id: dict[str, Vacancy] = {}
    for row in rows:
        published_at_raw = row["published_at"]
        published_at = datetime.fromisoformat(published_at_raw) if published_at_raw else None
        by_id[row["vacancy_id"]] = Vacancy(
            vacancy_id=row["vacancy_id"],
            url=row["url"] or "",
            title=row["title"] or "",
            company=row["company"] or "",
            salary_raw=row["salary_raw"] or "",
            salary_from=row["salary_from"],
            salary_to=row["salary_to"],
            area=row["area"] or "",
            snippet=row["snippet"] or "",
            description=row["description"] or "",
            activity_text=row["activity_text"] or "",
            published_at=published_at,
            date_unknown=bool(row["date_unknown"]),
        )

    # Preserve caller order.
    return [by_id[vacancy_id] for vacancy_id in vacancy_ids if vacancy_id in by_id]


def get_vacancy_by_id(db_path: Path, vacancy_id: str) -> Vacancy | None:
    rows = get_vacancies_by_ids(db_path, [vacancy_id])
    return rows[0] if rows else None
