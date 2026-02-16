from __future__ import annotations

from hh_monitor.app import MainWindow


def test_format_deep_full_text_html_adds_blank_before_heading() -> None:
    text = "Первая строка\nВаши задачи:\nТело"

    rendered = MainWindow._format_deep_full_text_html(None, text)

    expected = (
        "Первая строка<br><br>"
        "<span style='color:#E6C15A; font-weight:700;'>Ваши задачи:</span>"
    )
    assert expected in rendered


def test_format_deep_full_text_html_renders_bold_and_joins_split_commas() -> None:
    text = "Опыт\n**Генерального**\n,\n**Операционного**"

    rendered = MainWindow._format_deep_full_text_html(None, text)

    expected = (
        "Опыт<br>"
        "<span style='font-weight:700; color:#F0F0F3;'>Генерального</span>, "
        "<span style='font-weight:700; color:#F0F0F3;'>Операционного</span>"
    )
    assert expected in rendered
