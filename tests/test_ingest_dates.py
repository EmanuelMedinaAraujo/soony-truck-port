from core.ingest import parse_as_of


def test_weekday_in_a_status_title_resolves_to_the_launch_week_from_config():
    assert parse_as_of("Launch status · Saturday 13:00 · DC") == "2026-11-21T13:00:00+01:00"
    assert parse_as_of("Sunday 9:30") == "2026-11-22T09:30:00+01:00"


def test_explicit_dates_win_and_unknown_titles_give_none():
    assert parse_as_of("status 2026-11-20 18:00") == "2026-11-20T18:00:00+01:00"
    assert parse_as_of("no date here") is None
