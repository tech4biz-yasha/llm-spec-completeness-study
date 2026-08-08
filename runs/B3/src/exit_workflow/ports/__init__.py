"""Outbound ports.

Each protocol here marks a boundary the spec kit describes but does not specify in
implementable detail (the payment gateway, the UAE object store, the NOC document
template, the event bus, the exit-reason reference data). Keeping them as ports means
this module contains no invented behaviour for any of them, and each can be supplied by
whoever owns the real decision.
"""

from .events import Event, EventPublisher, PublishError
from .payments import GatewayResult, PaymentGateway
from .reference import ExitReasonReference
from .renderer import NocContext, NocRenderer
from .storage import ObjectStorage, StoredObject

__all__ = [
    "Event",
    "EventPublisher",
    "ExitReasonReference",
    "GatewayResult",
    "NocContext",
    "NocRenderer",
    "ObjectStorage",
    "PaymentGateway",
    "PublishError",
    "StoredObject",
]
