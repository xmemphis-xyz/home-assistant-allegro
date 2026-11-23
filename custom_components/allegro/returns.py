"""Buyer returns (rescissions) read from an order group's details."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .const import ALLEGRO_ORDERS_URL

_LOGGER = logging.getLogger(__name__)

# Timeline step that ends a return: the seller has decided about it.
TIMELINE_FINISHED = "FINISHED"

_CARRIER = re.compile(r"^\s*Przewo[źz]nik:\s*(.+?)\s*$", re.IGNORECASE)
_WAYBILL = re.compile(r"^\s*Numer przesy[łl]ki:\s*(.+?)\s*$", re.IGNORECASE)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _amount(value: Any) -> float:
    try:
        return float(_as_dict(value).get("amount") or 0)
    except TypeError, ValueError:
        return 0.0


def _datetime(value: Any) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _line_texts(lines: Any) -> list[str]:
    """Plain text of Allegro's `{content, type}` display lines."""
    texts = []
    for line in _as_list(lines):
        content = (
            _text(_as_dict(line).get("content"))
            if isinstance(line, dict)
            else _text(line)
        )
        if content:
            texts.append(content)
    return texts


@dataclass(frozen=True, slots=True)
class ReturnedItem:
    """One product sent back in a return."""

    name: str
    quantity: int
    unit_price: float
    currency: str
    reason: str | None

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> ReturnedItem:
        """Create an item from a rescission `offers` entry."""
        price = _as_dict(item.get("price"))
        try:
            quantity = int(item.get("quantity") or 1)
        except TypeError, ValueError:
            quantity = 1
        return cls(
            name=str(
                item.get("name") or _as_dict(item.get("product")).get("name") or ""
            ),
            quantity=quantity,
            unit_price=_amount(price),
            currency=str(price.get("currency") or ""),
            reason=_text(_as_dict(item.get("reason")).get("label")),
        )


@dataclass(frozen=True, slots=True)
class ReturnParcel:
    """A parcel the buyer sends back to the seller."""

    ordinal: int
    carrier: str | None
    waybill: str | None
    last_event: str | None
    last_event_date: str | None
    last_event_location: str | None
    refund: tuple[str, ...]

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> ReturnParcel:
        """Create a parcel from a `returnParcels` entry.

        The dispatch code (`parcelDispatchInfo`) lets anyone hand the parcel in,
        so it is left out on purpose, like the pickup codes of incoming parcels.
        """
        tracking = _as_dict(item.get("tracking"))
        carrier = waybill = None
        for text in _line_texts(tracking.get("lines")):
            if match := _CARRIER.match(text):
                carrier = match.group(1)
            elif match := _WAYBILL.match(text):
                waybill = match.group(1)
        history = [_as_dict(event) for event in _as_list(tracking.get("history"))]
        last = history[-1] if history else {}
        try:
            ordinal = int(item.get("ordinal") or 0)
        except TypeError, ValueError:
            ordinal = 0
        return cls(
            ordinal=ordinal,
            carrier=carrier,
            waybill=waybill,
            last_event=_text(last.get("description")),
            last_event_date=_text(last.get("date")),
            last_event_location=_text(last.get("location")),
            refund=tuple(_line_texts(item.get("paymentLines"))),
        )

    def as_attributes(self) -> dict[str, Any]:
        """State attributes for one return parcel."""
        return {
            "carrier": self.carrier,
            "waybill": self.waybill,
            "last_event": self.last_event,
            "last_event_date": self.last_event_date,
            "last_event_location": self.last_event_location,
            "refund": list(self.refund),
        }


@dataclass(frozen=True, slots=True)
class Return:
    """A return the buyer filed for (part of) an order."""

    id: str
    group_id: str
    seller: str
    reference_number: str | None
    status: str | None
    timeline_status: str | None
    timeline_label: str | None
    timeline_hint: str | None
    requested_at: datetime | None
    send_by: datetime | None
    items: tuple[ReturnedItem, ...]
    parcels: tuple[ReturnParcel, ...]

    @classmethod
    def from_api(cls, group_id: str, seller: str, item: dict[str, Any]) -> Return:
        """Create a return from a `rescissions.rescissions` entry."""
        timeline = _as_dict(item.get("timelineStatus"))
        offers = _as_list(item.get("offers")) or _as_list(item.get("rescissionOffers"))
        parcels = sorted(
            (
                ReturnParcel.from_api(_as_dict(parcel))
                for parcel in _as_list(item.get("returnParcels"))
            ),
            key=lambda parcel: parcel.ordinal,
        )
        return cls(
            id=str(item.get("rescissionId") or ""),
            group_id=group_id,
            seller=seller,
            reference_number=_text(item.get("referenceNumber")),
            status=_text(item.get("status")),
            timeline_status=_text(timeline.get("status")),
            timeline_label=_text(timeline.get("label")),
            timeline_hint=_text(timeline.get("hint")),
            requested_at=_datetime(item.get("requestDate")),
            send_by=_datetime(item.get("shipmentExpirationDate")),
            items=tuple(ReturnedItem.from_api(_as_dict(offer)) for offer in offers),
            parcels=tuple(parcels),
        )

    @property
    def is_active(self) -> bool:
        """Whether the seller has not decided about the return yet."""
        return self.timeline_status != TIMELINE_FINISHED

    @property
    def value(self) -> float:
        """Price of the returned products."""
        return round(sum(item.unit_price * item.quantity for item in self.items), 2)

    def as_attributes(self) -> dict[str, Any]:
        """State attributes for one return."""
        currency = next((item.currency for item in self.items if item.currency), "")
        return {
            "reference_number": self.reference_number,
            "seller": self.seller,
            "status": self.timeline_status,
            "status_label": self.timeline_label,
            "status_hint": self.timeline_hint,
            "active": self.is_active,
            "requested_at": self.requested_at.isoformat()
            if self.requested_at
            else None,
            "send_by": self.send_by.isoformat() if self.send_by else None,
            "products": [item.name for item in self.items],
            "reasons": sorted({item.reason for item in self.items if item.reason}),
            "value": self.value,
            "currency": currency,
            "parcels": [parcel.as_attributes() for parcel in self.parcels],
            "url": f"{ALLEGRO_ORDERS_URL}/{self.group_id}",
        }


def parse_returns(payload: dict[str, Any]) -> list[Return]:
    """Parse the returns out of a `/myorder-api/myorders/{groupId}` response."""
    group_id = str(payload.get("groupId") or "")
    returns: list[Return] = []
    for order in _as_list(payload.get("myorders")):
        order = _as_dict(order)
        seller = str(_as_dict(order.get("seller")).get("login") or "")
        rescissions = _as_dict(order.get("rescissions")).get("rescissions")
        for item in _as_list(rescissions):
            try:
                returns.append(Return.from_api(group_id, seller, _as_dict(item)))
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning(
                    "Skipping Allegro return in group %s: %s", group_id, err
                )
    return returns
