from pathlib import Path

SKILL = Path(__file__).with_name("brainstorming.md")


def read_skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_file_exists():
    assert SKILL.is_file()


def test_name_and_question_rule():
    text = read_skill()
    assert "name: brainstorming" in text
    assert "Лимита на число вопросов нет" in text
    assert "на усмотрение разработчиков" in text


def test_names_existing_tools():
    text = read_skill()
    for name in (
        "search_notes",
        "view_note",
        "write_note",
        "replace_note_content",
        "sdms_get_devrequest",
    ):
        assert name in text


def test_spec_sections():
    text = read_skill()
    for heading in (
        "## Проблема",
        "## Цели и не-цели",
        "## Уже принятые решения",
        "## Подходы",
        "## Поведение и контракты",
        "## Открытые инженерные пункты",
        "## Приёмка",
    ):
        assert heading in text


def test_forbids_plan_and_code():
    text = read_skill()
    assert "План не пиши" in text
    assert "Код не пиши" in text


def test_no_private_tool_or_disk():
    text = read_skill()
    assert "brainstorm(" not in text
    assert "Fileshed" not in text
    assert "/app/backend/data" not in text
