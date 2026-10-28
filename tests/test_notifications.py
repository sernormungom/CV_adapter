"""Phase 3a tests: notification store + service.

External channels (email/SMS) are gated off in the test environment, so
`dispatch`/`notify` write the in-app row and record only ["inapp"].
"""

from backend.auth import service as auth_service
from backend.db.models import UserRole
from backend.notifications import service


def _user(db, role=UserRole.ta, email="ta@x.com", **kwargs):
    user, invite = auth_service.create_user(db, email=email, role=role, **kwargs)
    auth_service.accept_invite(db, invite, "Password123!x")
    return user


def test_create_and_list(db):
    ta = _user(db)
    n = service.create(
        db,
        user_id=ta.id,
        type=service.MATCH_FOUND,
        title="New match for alice",
        consultant_username="alice",
        job_id="job_1",
        payload={"score": 82.0},
    )
    assert n is not None and n.read is False
    assert n.channels_sent == ["inapp"]

    items = service.list_for_user(db, ta.id)
    assert len(items) == 1
    assert items[0].payload["score"] == 82.0
    assert service.unread_count(db, ta.id) == 1


def test_dedupe_skips_duplicate(db):
    ta = _user(db)
    key = "match_found:alice:job_1"
    first = service.create(
        db, user_id=ta.id, type=service.MATCH_FOUND, title="x", dedupe_key=key
    )
    second = service.create(
        db, user_id=ta.id, type=service.MATCH_FOUND, title="x", dedupe_key=key
    )
    assert first is not None
    assert second is None
    assert service.unread_count(db, ta.id) == 1


def test_dedupe_is_scoped_per_recipient(db):
    ta1 = _user(db, email="ta1@x.com")
    ta2 = _user(db, email="ta2@x.com")
    key = "match_found:alice:job_1"
    assert service.create(db, user_id=ta1.id, type="t", title="x", dedupe_key=key)
    # Same key, different recipient → not deduped.
    assert service.create(db, user_id=ta2.id, type="t", title="x", dedupe_key=key)


def test_mark_read_and_mark_all_read(db):
    ta = _user(db)
    a = service.create(db, user_id=ta.id, type="t", title="a")
    service.create(db, user_id=ta.id, type="t", title="b")
    assert service.unread_count(db, ta.id) == 2

    assert service.mark_read(db, ta.id, a.id) is True
    assert service.unread_count(db, ta.id) == 1

    assert service.mark_all_read(db, ta.id) == 1
    assert service.unread_count(db, ta.id) == 0


def test_notify_records_inapp_only_when_channels_disabled(db):
    ta = _user(db)
    n = service.notify(
        db,
        user_id=ta.id,
        type=service.CV_VIEWED,
        title="Hiring manager opened the CV",
        email_to="manager@client.com",
        sms_to="+46700000000",
    )
    assert n is not None
    # Email/SMS disabled in dev → only the in-app channel is recorded.
    assert n.channels_sent == ["inapp"]


def test_list_unread_only_and_limit(db):
    ta = _user(db)
    for i in range(3):
        service.create(db, user_id=ta.id, type="t", title=f"n{i}")
    items = service.list_for_user(db, ta.id, limit=2)
    assert len(items) == 2  # ordered newest-first, capped by limit
    read_me = service.list_for_user(db, ta.id)[0]
    service.mark_read(db, ta.id, read_me.id)
    assert len(service.list_for_user(db, ta.id, unread_only=True)) == 2
