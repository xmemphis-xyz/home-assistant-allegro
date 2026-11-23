"""Buyer returns, built from synthetic order group details."""

from custom_components.allegro.models import AllegroData, Cart, parse_orders
from custom_components.allegro.returns import parse_returns


def tracking(waybill, *events):
    return {
        "lines": [
            {"content": "Przewoźnik: Allegro", "type": "primary"},
            {"content": f"Numer przesyłki: {waybill}", "type": "primary"},
        ],
        "history": [
            {"date": date, "description": text, **({"location": loc} if loc else {})}
            for date, text, loc in events
        ],
    }


GROUP = {
    "groupId": "g1",
    "myorders": [
        {
            "seller": {"login": "shop_a"},
            "rescissions": {
                "rescissions": [
                    {
                        "rescissionId": "r1",
                        "status": "SHIPMENT_SELECTED",
                        "shipmentExpirationDate": "2026-12-05T22:59:59.734Z",
                        "requestDate": "2026-11-21T10:59:13.734Z",
                        "referenceNumber": "ABCD/2026",
                        "timelineStatus": {
                            "status": "DISPATCHED",
                            "label": "Zwrot nadany",
                            "hint": "21 lis 12:13",
                        },
                        "offers": [
                            {
                                "name": "Plates",
                                "quantity": 2,
                                "price": {"amount": "82.75", "currency": "PLN"},
                                "reason": {"label": "Nie pasuje mi"},
                            }
                        ],
                        "returnParcels": [
                            {
                                "ordinal": 2,
                                "parcelDispatchInfo": {"code": "111 222 333"},
                                "tracking": tracking(
                                    "AD2", ("22 lis 2026, 16:25", "Przygotowana", None)
                                ),
                                "paymentLines": [],
                            },
                            {
                                "ordinal": 1,
                                "parcelDispatchInfo": {"code": "444 555 666"},
                                "tracking": tracking(
                                    "AD1",
                                    ("21 lis 2026, 12:00", "Przygotowana", None),
                                    ("21 lis 2026, 12:13", "Nadana", "Piastów"),
                                ),
                                "paymentLines": [{"content": "Zwrócono 165,50 zł"}],
                            },
                        ],
                    }
                ]
            },
        }
    ],
}


def test_return_details_are_parsed():
    (item,) = parse_returns(GROUP)
    assert item.is_active
    assert item.value == 165.5
    attributes = item.as_attributes()
    assert attributes["reference_number"] == "ABCD/2026"
    assert attributes["status"] == "DISPATCHED"
    assert attributes["status_label"] == "Zwrot nadany"
    assert attributes["reasons"] == ["Nie pasuje mi"]
    assert attributes["send_by"].startswith("2026-12-05")
    assert attributes["url"].endswith("/g1")
    first, second = attributes["parcels"]
    assert (first["waybill"], first["carrier"]) == ("AD1", "Allegro")
    assert (first["last_event"], first["last_event_location"]) == ("Nadana", "Piastów")
    assert first["refund"] == ["Zwrócono 165,50 zł"]
    assert second["waybill"] == "AD2"


def test_dispatch_codes_stay_out_of_attributes():
    text = str(parse_returns(GROUP)[0].as_attributes())
    assert "111 222 333" not in text
    assert "444 555 666" not in text


def test_finished_return_and_orders_without_details():
    finished = {
        "groupId": "g1",
        "myorders": [
            {
                "seller": {"login": "shop_a"},
                "rescissions": {
                    "rescissions": [
                        {"rescissionId": "r1", "timelineStatus": {"status": "FINISHED"}}
                    ]
                },
            }
        ],
    }
    orders = parse_orders(
        {
            "orderGroups": [
                {
                    "groupId": "g1",
                    "rescissionId": "r1",
                    "myorders": [
                        {
                            "seller": {"login": "shop_a"},
                            "status": {"primary": {"status": "PARTIALLY_RETURNED"}},
                        }
                    ],
                },
                {
                    "groupId": "g2",
                    "myorders": [
                        {
                            "seller": {"login": "shop_b"},
                            "status": {"primary": {"status": "RETURNED_TO_SENDER"}},
                        }
                    ],
                },
                {
                    "groupId": "g3",
                    "myorders": [
                        {
                            "seller": {"login": "shop_c"},
                            "status": {"primary": {"status": "DELIVERED"}},
                        }
                    ],
                },
            ]
        }
    )
    data = AllegroData(
        orders=orders, cart=Cart.empty(), returns=tuple(parse_returns(finished))
    )
    assert data.groups_with_returns == ["g1", "g2"]
    assert data.active_returns == []
    rows = data.returns_details()
    assert [(row["seller"], row["status"], row["active"]) for row in rows] == [
        ("shop_a", "FINISHED", False),
        ("shop_b", "RETURNED_TO_SENDER", False),
    ]
