import pytest

from moppu.ingestion.youtube import parse_video_id


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share", "dQw4w9WgXcQ"),
    ],
)
def test_parse_video_id_variants(raw: str, expected: str) -> None:
    assert parse_video_id(raw) == expected


def test_parse_video_id_invalid() -> None:
    with pytest.raises(ValueError):
        parse_video_id("not-a-real-url-or-id")


def test_video_list_entry_stored(tmp_path):
    """VideoListEntry rows are created idempotently for each (list, video_id) pair."""
    from moppu.storage import create_engine_and_session, init_db
    from moppu.storage.db import VideoListEntry

    engine, Session = create_engine_and_session("sqlite:///:memory:")
    init_db(engine)

    with Session() as s:
        s.add(VideoListEntry(list_name="my-list", video_id="dQw4w9WgXcQ", source_url="https://youtu.be/dQw4w9WgXcQ"))
        s.commit()
        rows = s.query(VideoListEntry).filter_by(list_name="my-list").all()
        assert len(rows) == 1
        assert rows[0].video_id == "dQw4w9WgXcQ"
