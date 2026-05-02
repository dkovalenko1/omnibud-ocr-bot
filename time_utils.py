from datetime import datetime, timezone
from zoneinfo import ZoneInfo


KYIV_TZ = ZoneInfo("Europe/Kyiv")


def to_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware regardless of source tzinfo state."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def kyiv_message_date(message_date: datetime) -> datetime:
    return to_utc(message_date).astimezone(KYIV_TZ)
