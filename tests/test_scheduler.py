from datetime import datetime

from app.scheduler import add_months


def test_add_months():
    assert add_months(datetime(2026, 8, 31), 6) == datetime(2027, 2, 28)
    assert add_months(datetime(2026, 11, 6), 3) == datetime(2027, 2, 6)


def test_app_password_spaces_removed():
    from app.main import normalize_password
    assert normalize_password("abcd efgh ijkl mnop") == "abcdefghijklmnop"
    assert normalize_password(" abcdefghijklmnop ") == "abcdefghijklmnop"
    assert normalize_password("my pass word") == "my pass word"
