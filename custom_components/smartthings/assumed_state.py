"""Assumed attribute values for commands sent while REST polling.

Without push, a command's effect isn't visible until the next poll, so the UI toggle snaps
back and a poll that was already in flight can overwrite the new state with the old one.
After a successful command we record the value it should produce, stamped with the time the
command was sent. That value is shown until a poll reports the attribute with a newer
*device-side* timestamp (``Status.timestamp``), or until it expires. Comparing against the
device's report time rather than the poll's arrival time is what keeps a stale in-flight
poll from winning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pysmartthings import Status

from .const import ASSUMED_STATE_VALIDITY

# (component, capability, attribute)
type AssumedKey = tuple[str, str, str]


@dataclass(slots=True)
class AssumedValue:
    """A value a command is expected to produce, and when the command was sent."""

    value: Any
    sent_at: datetime


def resolve_attribute_value(
    assumed: dict[AssumedKey, AssumedValue],
    key: AssumedKey,
    status: Status,
    now: datetime,
) -> Any:
    """Return the assumed value while it's still authoritative, else the polled one.

    Drops the assumed entry once it has expired or a newer device report has arrived.
    """
    if (entry := assumed.get(key)) is not None:
        if now - entry.sent_at < timedelta(seconds=ASSUMED_STATE_VALIDITY) and (
            status.timestamp is None or status.timestamp < entry.sent_at
        ):
            return entry.value
        del assumed[key]
    return status.value
