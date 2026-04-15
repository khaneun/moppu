from moppu.storage import create_engine_and_session, init_db
from moppu.storage.db import Channel


def test_schema_creates_and_round_trips():
    engine, Session = create_engine_and_session("sqlite:///:memory:")
    init_db(engine)
    with Session() as s:
        s.add(Channel(channel_id="UC123", name="test", tags=["a"], enabled=True))
        s.commit()
        row = s.query(Channel).filter_by(channel_id="UC123").one()
        assert row.name == "test"
        assert row.tags == ["a"]
