from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.database import Base
from app.services.scheduled_delivery_dedup import claim_scheduled_delivery


def test_scheduled_delivery_is_claimed_once_per_chat_and_key() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    assert claim_scheduled_delivery(
        session,
        dedup_key="scheduled:smxm:morning:2026-08-10",
        chat_id=1517974707,
    )
    assert not claim_scheduled_delivery(
        session,
        dedup_key="scheduled:smxm:morning:2026-08-10",
        chat_id=1517974707,
    )

    assert claim_scheduled_delivery(
        session,
        dedup_key="scheduled:smxm:morning:2026-08-10",
        chat_id=5200119302,
    )
    assert claim_scheduled_delivery(
        session,
        dedup_key="scheduled:smxm:evening:2026-08-10",
        chat_id=1517974707,
    )
