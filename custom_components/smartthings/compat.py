"""Compatibility shims for running upstream SmartThings code on older HA cores.

Upstream HA core targets only its own release. This custom integration supports
older cores too (see ``hacs.json``), so newer core APIs that upstream code uses
are routed through here. On a core that has the API these helpers call it
directly; on an older core they fall back to the equivalent older call.

- HA 2026.7 added ``UnitOfDensity`` / ``UnitOfRatio``.
- HA 2026.8 made each device belong to exactly one config entry and added
  ``async_get_device_by_identifier``, ``async_get_device_id_by_identifier``,
  ``DeviceInfo.via_device_id`` and the matching deprecations.

Remove a shim once ``hacs.json``'s minimum HA version includes its API.
"""

from enum import StrEnum

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

try:
    from homeassistant.const import UnitOfDensity, UnitOfRatio
except ImportError:  # HA < 2026.7

    class UnitOfDensity(StrEnum):  # type: ignore[no-redef]
        """Density units (subset used by this integration)."""

        MICROGRAMS_PER_CUBIC_METER = "μg/m³"

    class UnitOfRatio(StrEnum):  # type: ignore[no-redef]
        """Ratio units (subset used by this integration)."""

        PARTS_PER_MILLION = "ppm"
        PERCENTAGE = "%"


# HA 2026.8+: devices are owned by a single config entry, and identifier lookups are
# scoped to that entry.
SINGLE_ENTRY_DEVICE_REGISTRY = hasattr(
    dr.DeviceRegistry, "async_get_device_by_identifier"
)


def async_get_device_by_identifier(
    device_registry: dr.DeviceRegistry,
    identifier: tuple[str, str],
    config_entry_id: str,
) -> dr.DeviceEntry | None:
    """Return the config entry's device with this identifier."""
    if SINGLE_ENTRY_DEVICE_REGISTRY:
        return device_registry.async_get_device_by_identifier(
            identifier, config_entry_id
        )
    device = device_registry.async_get_device(identifiers={identifier})
    if device is None or config_entry_id not in device.config_entries:
        return None
    return device


def async_remove_device(
    device_registry: dr.DeviceRegistry, device_id: str, config_entry_id: str
) -> None:
    """Remove a device that belongs to this config entry.

    On older cores a device can be shared by several config entries, so only this
    entry is detached. ``async_remove_device`` there would also delete it for the
    other integrations.
    """
    if SINGLE_ENTRY_DEVICE_REGISTRY:
        device_registry.async_remove_device(device_id)
    else:
        device_registry.async_update_device(
            device_id, remove_config_entry_id=config_entry_id
        )


def via_device_info(
    hass: HomeAssistant, identifier: tuple[str, str], config_entry_id: str
) -> dict[str, str | tuple[str, str]]:
    """Return the ``DeviceInfo`` keys that link a device to its via device."""
    if SINGLE_ENTRY_DEVICE_REGISTRY:
        return {
            "via_device_id": dr.async_get_device_id_by_identifier(
                hass, identifier, config_entry_id=config_entry_id
            )
        }
    return {"via_device": identifier}
