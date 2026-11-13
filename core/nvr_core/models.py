"""Database models for the core: recorded webhook events."""

from datetime import UTC, date, datetime, time, timedelta
from typing import cast

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import DateTime, Integer, String, Text, delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)


class Event(Base):
    """One webhook push received from the gateway (keepalives excluded)."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    """``alarm``, ``snapshot``, or ``unknown``."""

    device_name: Mapped[str | None] = mapped_column(String, nullable=True)
    str_chn: Mapped[str | None] = mapped_column(String, nullable=True)
    """Best-effort channel label (e.g. ``CH2``)."""

    object_snap: Mapped[str | None] = mapped_column(String, nullable=True)
    background_snap: Mapped[str | None] = mapped_column(String, nullable=True)
    """Stored snapshot file names (unguessable; served via ``/snaps/``)."""

    live_snaps: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    """JSON list of instant-capture file names (go2rtc), stored at alarm time."""

    group_token: Mapped[str | None] = mapped_column(String, nullable=True)
    """Correlation token: an alarm and the AI snapshots that follow it within
    the configured window share a group (the notification's click target)."""

    payload: Mapped[str] = mapped_column(Text, nullable=False)
    """The push body as redacted JSON (embedded images elided, truncated)."""

    @property
    def live_snaps_parsed(self) -> list[str]:
        """The instant-capture file names (decoded from JSON storage)."""
        import json

        try:
            parsed = json.loads(self.live_snaps)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []

    @classmethod
    def prune(cls, retention_days: int) -> int:
        """Delete events older than ``retention_days``; 0 disables."""
        if retention_days <= 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        result = cast(CursorResult, db.session.execute(delete(cls).where(cls.received_at < cutoff)))
        db.session.commit()
        return result.rowcount


def recent_group(window_s: int) -> str | None:
    """The group token of the most recent alarm within ``window_s`` seconds.

    Correlation is by time alone: the group page shows every image in the
    window anyway, and a single NVR's channels fire near-simultaneously.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=window_s)
    event = db.session.execute(
        select(Event)
        .where(
            Event.kind == "alarm",
            Event.group_token.is_not(None),
            Event.received_at >= cutoff,
        )
        .order_by(Event.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return event.group_token if event else None


def events_for_group(token: str) -> list[Event]:
    """All events of one group, oldest first."""
    return list(
        db.session.execute(select(Event).where(Event.group_token == token).order_by(Event.id))
        .scalars()
        .all()
    )


def _to_local_date(received_at: datetime | None) -> date | None:
    """A stored ``received_at`` as a host-local calendar date.

    ``None`` stays ``None``; naive values are UTC (SQLite round-trip).
    """
    if received_at is None:
        return None
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=UTC)
    return received_at.astimezone().date()


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """The UTC bounds ``[start, end)`` of the host-local calendar day ``day``.

    Days are bucketed home-local, like the rendered times: which day an
    event belongs to must not depend on the storage timezone.
    """
    start = datetime.combine(day, time.min).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min).astimezone(UTC)
    return start, end


def events_for_day(day: date) -> list[Event]:
    """The events of one host-local calendar day, newest first."""
    start, end = _day_bounds(day)
    return list(
        db.session.execute(
            select(Event)
            .where(Event.received_at >= start, Event.received_at < end)
            .order_by(Event.id.desc())
        )
        .scalars()
        .all()
    )


def latest_event_day() -> date | None:
    """The host-local day of the newest event, if any."""
    newest = db.session.execute(select(func.max(Event.received_at))).scalar()
    return _to_local_date(newest)


def adjacent_event_days(day: date) -> tuple[date | None, date | None]:
    """The nearest days that hold events before and after ``day``.

    Skipping empty days keeps "previous"/"next" meaningful however quiet
    the system was.
    """
    start, end = _day_bounds(day)
    before = db.session.execute(
        select(func.max(Event.received_at)).where(Event.received_at < start)
    ).scalar()
    after = db.session.execute(
        select(func.min(Event.received_at)).where(Event.received_at >= end)
    ).scalar()
    return _to_local_date(before), _to_local_date(after)


def get_event(event_id: int) -> Event | None:
    return db.session.get(Event, event_id)
