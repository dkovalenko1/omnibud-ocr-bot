from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


try:
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except ZoneInfoNotFoundError:
    # Windows needs the PyPI tzdata package for IANA zones. Keep the bot
    # importable if deploy has not installed new requirements yet.
    KYIV_TZ = timezone(timedelta(hours=2))


def to_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware regardless of source tzinfo state."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def kyiv_message_date(message_date: datetime) -> datetime:
    return to_utc(message_date).astimezone(KYIV_TZ)
