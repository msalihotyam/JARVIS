"""Deterministic connector selection based on explicit ranking signals."""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.connectors.base import BaseConnector
from jarvis.contracts import Availability, CostEstimate, Task


class ConnectorNotFoundError(LookupError):
    pass


class ConnectorUnavailableError(LookupError):
    pass


class AmbiguousConnectorError(LookupError):
    pass


class DuplicateConnectorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ConnectorSelection:
    connector: BaseConnector
    suitability: float
    availability: Availability
    estimate: CostEstimate


class ToolRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, BaseConnector] = {}

    def register(self, connector: BaseConnector) -> None:
        name = connector.manifest.name
        if name in self._connectors:
            raise DuplicateConnectorError(f"Connector already registered: {name}")
        self._connectors[name] = connector

    def get(self, name: str) -> BaseConnector:
        try:
            return self._connectors[name]
        except KeyError as exc:
            raise ConnectorNotFoundError(name) from exc

    def select(self, task: Task) -> ConnectorSelection:
        preferred = task.preferences.get("preferred_connector")
        if preferred:
            connector = self.get(str(preferred))
            if not connector.can_handle(task):
                raise ConnectorNotFoundError(
                    f"Preferred connector cannot handle {task.capability}: {preferred}"
                )
            availability = connector.availability()
            if not availability.available:
                raise ConnectorUnavailableError(availability.reason or str(preferred))
            return ConnectorSelection(
                connector=connector,
                suitability=connector.suitability(task),
                availability=availability,
                estimate=connector.estimate_cost(task),
            )

        capable = [connector for connector in self._connectors.values() if connector.can_handle(task)]
        if not capable:
            raise ConnectorNotFoundError(
                f"No connector can handle capability: {task.capability}"
            )

        candidates: list[ConnectorSelection] = []
        unavailable_reasons: list[str] = []
        for connector in capable:
            availability = connector.availability()
            if not availability.available:
                unavailable_reasons.append(
                    f"{connector.manifest.name}: {availability.reason or 'unavailable'}"
                )
                continue
            suitability = float(connector.suitability(task))
            if suitability <= 0:
                continue
            candidates.append(
                ConnectorSelection(
                    connector=connector,
                    suitability=suitability,
                    availability=availability,
                    estimate=connector.estimate_cost(task),
                )
            )

        if not candidates:
            raise ConnectorUnavailableError(
                "; ".join(unavailable_reasons) or "No suitable connector is available"
            )

        candidates.sort(
            key=lambda candidate: (
                -candidate.connector.manifest.priority,
                -candidate.suitability,
                candidate.estimate.maximum_usd,
                candidate.connector.manifest.name,
            )
        )
        best = candidates[0]
        best_rank = (
            best.connector.manifest.priority,
            round(best.suitability, 9),
            round(best.estimate.maximum_usd, 9),
        )
        tied = [
            candidate
            for candidate in candidates
            if (
                candidate.connector.manifest.priority,
                round(candidate.suitability, 9),
                round(candidate.estimate.maximum_usd, 9),
            )
            == best_rank
        ]
        if len(tied) > 1:
            names = ", ".join(candidate.connector.manifest.name for candidate in tied)
            raise AmbiguousConnectorError(f"Connector selection is ambiguous: {names}")
        return best

    def list_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._connectors))

