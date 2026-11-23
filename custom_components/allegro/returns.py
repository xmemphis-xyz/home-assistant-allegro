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

# Words of a return method id such as `free_ad_dpd_pickup_method`.
_METHOD_WORDS = {
    "ad": "Allegro Delivery",
    "dpd": "DPD",
    "inpost": "InPost",
    "orlen": "ORLEN Paczka",
    "pocztex": "Pocztex",
    "poczta": "Poczta Polska",
    "dhl": "DHL",
    "ups": "UPS",
    "gls": "GLS",
    "fedex": "FedEx",
    "one": "One Box",
    "box": "",
    "pickup": "Pickup",
    "locker": "automat",
    "lockers": "automat",
    "paczkomat": "Paczkomat",
    "courier": "kurier",
    "kurier": "kurier",
    "self": "własna wysyłka",
}


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


def _flatten(node: Any) -> str:
    """Plain text of a rich-text node: a string, a `{content}` or `{children}`."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_flatten(child) for child in node)
    if not isinstance(node, dict):
        return ""
    if "content" in node:
        return str(node.get("content") or "")
    return _flatten(node.get("children"))


def _instructions(nodes: Any) -> list[str]:
    """One line per list item (or paragraph) of Allegro's sending instructions."""
    lines: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        node = _as_dict(node)
        if node.get("type") == "list":
            walk(node.get("children"))
            return
        if text := " ".join(_flatten(node).split()):
            lines.append(text)

    walk(nodes)
    return lines


def method_label(method_id: str | None) -> str | None:
    """Readable name of a return method id, for example `Allegro Delivery DPD`."""
    if not method_id:
        return None
    words = [w for w in method_id.lower().split("_") if w not in ("method", "")]
    free = "free" in words
    names = [
        _METHOD_WORDS.get(w, w.upper() if len(w) <= 4 else w)
        for w in words
        if w != "free"
    ]
    label = " ".join(name for name in names if name)
    return f"{label} (bezpłatnie)" if free else label or method_id


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
    method: str | None = None
    dispatch_code: str | None = None
    dispatch_code_info: tuple[str, ...] = ()
    instructions: tuple[str, ...] = ()

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> ReturnParcel:
        """Create a parcel from a `returnParcels` entry."""
        dispatch = _as_dict(item.get("parcelDispatchInfo"))
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
            method=_text(item.get("returnMethodId")),
            dispatch_code=_text(dispatch.get("code")),
            dispatch_code_info=tuple(
                _line_texts(dispatch.get("prefix"))
                + _line_texts(dispatch.get("suffix"))
            ),
            instructions=tuple(_instructions(item.get("instructionLines"))),
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
            "method": self.method,
            "method_label": method_label(self.method),
            "dispatch_code": self.dispatch_code,
            "dispatch_code_info": list(self.dispatch_code_info),
            "instructions": list(self.instructions),
        }


def _steps(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    """Timeline steps marked `done`, `active` or `todo`.

    Allegro flags only the current step; the ones before it are done.
    """
    raw = [_as_dict(step) for step in _as_list(timeline.get("steps"))]
    current = next((i for i, step in enumerate(raw) if step.get("active")), None)
    if current is None and timeline.get("status") == TIMELINE_FINISHED:
        current = len(raw)
    steps = []
    for index, step in enumerate(raw):
        if current is None or index > current:
            state = "todo"
        elif index == current:
            state = "active"
        else:
            state = "done"
        steps.append(
            {
                "type": _text(step.get("type")),
                "label": _text(step.get("label")),
                "hint": _text(step.get("hint")),
                "state": state,
            }
        )
    return steps


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
    steps: tuple[dict[str, Any], ...] = ()

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
            steps=tuple(_steps(timeline)),
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
            "steps": [dict(step) for step in self.steps],
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
