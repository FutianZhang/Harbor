"""Frozen 30-item rubric to eight positive programmatic capabilities."""
from __future__ import annotations

from collections.abc import Iterable


PROGRAMMATIC_MAPPING: dict[str, tuple[str, ...]] = {
    "P01": ("POS-1", "POS-2", "POS-3"),
    "P02": ("PRC-1", "PRC-2", "PRC-3"),
    "P03": ("GRK-1", "GRK-2", "GRK-3", "GRK-4", "GRK-5"),
    "P04": ("PNL-1", "PNL-2", "PNL-3"),
    "P05": ("ATT-1", "ATT-2", "ATT-3", "ATT-4"),
    "P06": ("STR-1", "STR-2", "STR-3"),
    "P07": ("VAL-1", "VAL-2", "VAL-3", "VAL-4"),
    "P08": ("OUT-1", "OUT-2", "OUT-3", "OUT-4", "OUT-5"),
}

ATOMIC_ITEMS: tuple[str, ...] = tuple(
    item for items in PROGRAMMATIC_MAPPING.values() for item in items
)


def programmatic_values(failed_items: Iterable[str]) -> dict[str, float]:
    """Return binary capability credit from the authoritative atomic failures."""
    failed = frozenset(failed_items)
    return {
        programmatic_id: 0.0 if failed.intersection(atomic_items) else 1.0
        for programmatic_id, atomic_items in PROGRAMMATIC_MAPPING.items()
    }

