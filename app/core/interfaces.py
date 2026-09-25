"""
AUSTRO AI - Cross-cutting interfaces.

Thin interfaces that decouple the application layer from concrete
implementations (AI providers, persistence, scheduling).
"""

from __future__ import annotations

import abc

from app.domain.ai import AIRequest


class AIProvider(abc.ABC):
    """A provider capable of generating text for a given AI request.

    Capabilities are routed by `request.capability` / `request.category`,
    not by provider identity.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable provider name (for telemetry)."""

    @property
    @abc.abstractmethod
    def is_available(self) -> bool:
        """Whether the provider can currently be used."""

    @abc.abstractmethod
    async def generate(self, request: AIRequest) -> str:
        """Generate a text response for the request. Raises AUSTRO errors."""