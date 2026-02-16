from __future__ import annotations

from hh_monitor.filters import filter_vacancy
from hh_monitor.models import Vacancy


def test_filter_vacancy_include_exclude_and_salary() -> None:
    vacancy = Vacancy(
        vacancy_id="1",
        url="https://hh.ru/vacancy/1",
        title="Chief Operating Officer",
        company="ACME",
        snippet="Трансформация",
        description="Операционная стратегия",
        salary_from=600000,
        salary_to=800000,
    )
    matched, reason = filter_vacancy(
        vacancy,
        include_keywords=["COO", "Chief"],
        exclude_keywords=["junior"],
        min_salary=500000,
        include_description=True,
    )
    assert matched is True
    assert "salary:>=500000" in reason


def test_filter_vacancy_respects_exclude() -> None:
    vacancy = Vacancy(
        vacancy_id="2",
        url="https://hh.ru/vacancy/2",
        title="Junior assistant",
        company="ACME",
        snippet="без опыта",
    )
    matched, reason = filter_vacancy(
        vacancy,
        include_keywords=["assistant"],
        exclude_keywords=["junior", "без опыта"],
        min_salary=None,
        include_description=False,
    )
    assert matched is False
    assert "exclude" in reason


def test_filter_vacancy_matches_russian_inflections() -> None:
    vacancy = Vacancy(
        vacancy_id="3",
        url="https://hh.ru/vacancy/3",
        title="Ищем исполнительного директора для трансформации компании",
        company="ACME",
        snippet="Опыт директора в B2B обязателен",
    )
    matched, reason = filter_vacancy(
        vacancy,
        include_keywords=["исполнительный директор", "директор по трансформации"],
        exclude_keywords=[],
        min_salary=None,
        include_description=False,
    )
    assert matched is True
    assert "include:title" in reason or "include:snippet" in reason


def test_filter_vacancy_matches_english_inflections() -> None:
    vacancy = Vacancy(
        vacancy_id="4",
        url="https://hh.ru/vacancy/4",
        title="Directors of Operations for international growth",
        company="Globex",
        snippet="Operations leadership role",
    )
    matched, _ = filter_vacancy(
        vacancy,
        include_keywords=["Director of Operations"],
        exclude_keywords=[],
        min_salary=None,
        include_description=False,
    )
    assert matched is True


def test_filter_vacancy_exclude_matches_inflected_phrase() -> None:
    vacancy = Vacancy(
        vacancy_id="5",
        url="https://hh.ru/vacancy/5",
        title="Коммерческий директор",
        company="Example",
        snippet="Подойдет кандидату с опытом руководителя отдела продаж",
    )
    matched, reason = filter_vacancy(
        vacancy,
        include_keywords=["директор"],
        exclude_keywords=["руководитель отдела продаж"],
        min_salary=None,
        include_description=False,
    )
    assert matched is False
    assert 'exclude:snippet("руководитель отдела продаж")' in reason
