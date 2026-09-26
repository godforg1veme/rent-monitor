"""Independent HTTP collectors for public rental-search pages."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rent_monitor.collectors.avito import AvitoCollector
from rent_monitor.collectors.cian import CianCollector
from rent_monitor.collectors.domclick import DomclickCollector
from rent_monitor.collectors.yandex import YandexCollector
from rent_monitor.config import RuntimeConfig

if TYPE_CHECKING:
    from rent_monitor.core.scheduler import Collector


def build_collectors(config: RuntimeConfig) -> list[Collector]:
    """Build enabled source collectors in the stable configuration order."""
    factories = {
        "avito": AvitoCollector,
        "cian": CianCollector,
        "domclick": DomclickCollector,
        "yandex": YandexCollector,
    }
    collectors: list[Collector] = []
    for source in config.sources:
        if not source.enabled:
            continue
        if source.name == "avito":
            collectors.append(AvitoCollector(source.searches))
        else:
            collectors.append(factories[source.name]())
    return collectors


__all__ = [
    "AvitoCollector",
    "CianCollector",
    "DomclickCollector",
    "YandexCollector",
    "build_collectors",
]
