"""The WhatsApp conversation (services/whatsapp/bot.py): pure, no database.

Each test plays a customer through ``step`` and checks what the bot says
and which action it asks service.py to run."""
from decimal import Decimal

import pytest

from services.whatsapp import bot
from services.whatsapp.client import payload, readable

ITEMS = [
    {"id": 1, "name": "Sugar 1kg", "description": "White sugar", "unit": "pack",
     "category": "Groceries", "icon": "fa-box", "price": 150.0, "stock": "in"},
    {"id": 2, "name": "Bar soap", "description": "", "unit": "", "category": "Household",
     "icon": "fa-box", "price": 80.0, "stock": "few"},
    {"id": 3, "name": "T-shirt", "description": "", "unit": "", "category": "Clothes",
     "icon": "fa-box", "price": 800.0, "stock": "in",
     "sizes": [{"id": 31, "label": "M", "price": 900.0, "stock": "in"},
               {"id": 32, "label": "L", "price": 800.0, "stock": "few"}]},
    {"id": 4, "name": "Brown sugar 2kg", "description": "", "unit": "pack",
     "category": "Groceries", "icon": "fa-box", "price": 320.0, "stock": "in"},
]


def ctx(**kw):
    base = dict(business="Duka Bora", items=ITEMS, phone="0712 345 678",
                profile_name="Achieng")
    base.update(kw)
    return bot.Ctx(**base)


def typed(t):
    return bot.Event("text", text=t)


def tap(choice, title=""):
    return bot.Event("choice", choice=choice, text=title)


class Chat:
    """A customer talking to the bot, keeping the state between messages."""

    def __init__(self, **ctx_kw):
        self.ctx = ctx(**ctx_kw)
        self.state = {}
        self.last = None

    def send(self, event):
        self.last = bot.step(self.state, event, self.ctx)
        self.state = self.last.state
        return self.last

    def body(self):
        return "\n".join(readable(m) for m in self.last.messages)

    def ids(self):
        out = []
        for m in self.last.messages:
            if m["type"] == "buttons":
                out += [b[0] for b in m["buttons"]]
            if m["type"] == "list":
                out += [r[0] for r in m["rows"]]
        return out


# --- greeting and menu ------------------------------------------------------------------

def test_hi_shows_the_menu():
    c = Chat()
    c.send(typed("Hi"))
    assert "Welcome to Duka Bora" in c.body()
    assert c.ids() == ["m:shop", "m:orders", "m:talk"]


def test_a_first_message_that_names_a_product_searches_straight_away():
    c = Chat()
    c.send(typed("sugar"))
    assert set(c.ids()) == {"i:1", "i:4"}


def test_a_first_message_that_matches_nothing_greets():
    c = Chat()
    c.send(typed("Good morning, are you open?"))
    assert "Welcome" in c.body()


def test_closed_shop_offers_orders_and_a_person_only():
    c = Chat(shop_open=False, items=[])
    c.send(typed("hi"))
    assert "not taking orders" in c.body()
    assert c.ids() == ["m:orders", "m:talk"]
    c.send(tap("i:1"))                      # an old button cannot buy
    assert c.ids() == ["m:orders", "m:talk"]


# --- browsing ---------------------------------------------------------------------------

def test_shop_lists_categories_then_items_with_prices():
    c = Chat()
    c.send(tap("m:shop"))
    assert c.ids() == ["c:Clothes", "c:Groceries", "c:Household"]
    c.send(tap("c:Groceries"))
    body = c.body()
    assert "Brown sugar 2kg" in body and "KES 320" in body and "Sugar 1kg" in body
    assert c.ids() == ["i:1", "i:4"]                      # catalogue order (by name)


def test_long_lists_page_with_a_more_row():
    many = [{"id": n, "name": f"Item {n:02d}", "description": "", "unit": "",
             "category": "Bulk", "icon": "", "price": 10.0, "stock": "in"} for n in range(1, 24)]
    c = Chat(items=many)
    c.send(tap("m:shop"))                   # one category: straight to its items
    assert len(c.ids()) == 10 and c.ids()[-1] == "ip:1"
    c.send(tap("ip:1"))
    assert c.ids()[0] == "i:10" and c.ids()[-1] == "ip:2"
    c.send(tap("ip:2"))
    assert c.ids() == [f"i:{n}" for n in range(19, 24)]


def test_every_list_fits_whatsapps_limits():
    long = [{"id": n, "name": "A very long product name that goes on and on " * 2,
             "description": "x" * 300, "unit": "", "category": f"Category with a long name {n}",
             "icon": "", "price": 1234567.5, "stock": "in"} for n in range(1, 40)]
    c = Chat(items=long)
    for event in (tap("m:shop"), tap("cp:1"), tap("c:Category with a long name 5"),
                  tap("i:5")):
        c.send(event)
        for m in c.last.messages:
            p = payload("254712345678", m)
            if p["type"] == "interactive":
                inter = p["interactive"]
                assert len(inter["body"]["text"]) <= 1024
                if inter["type"] == "list":
                    rows = inter["action"]["sections"][0]["rows"]
                    assert len(rows) <= 10
                    assert all(len(r["title"]) <= 24 and len(r.get("description", "")) <= 72
                               and len(r["id"]) <= 200 for r in rows)
                    assert len(inter["action"]["button"]) <= 20
                else:
                    assert all(len(b["reply"]["title"]) <= 20
                               for b in inter["action"]["buttons"])


def test_sizes_are_picked_before_quantity():
    c = Chat()
    c.send(tap("i:3"))
    assert c.ids() == ["s:3:31", "s:3:32"]
    c.send(tap("s:3:31"))
    assert "T-shirt (M): KES 900" in c.body() and c.ids() == ["q:1", "q:2", "q:3"]


def test_unknown_or_forged_ids_never_reach_the_cart():
    c = Chat()
    for forged in ("i:999", "s:3:99", "s:1:31", "q:5", "c:Nope", "r:7", "x:1", "i:abc"):
        c.send(tap(forged))
        assert not c.state.get("cart")


# --- cart -------------------------------------------------------------------------------

def add(c, item_id, qty):
    c.send(tap(f"i:{item_id}"))
    c.send(typed(str(qty)) if qty > 3 else tap(f"q:{qty}"))


def test_add_to_cart_by_tap_or_typed_number_and_totals():
    c = Chat()
    add(c, 1, 2)
    assert "Added 2 × Sugar 1kg" in c.body() and "KES 300" in c.body()
    add(c, 2, 5)
    assert "Cart: 7 items, KES 700" in c.body()
    add(c, 1, 1)                            # the same item again adds up
    assert [l["qty"] for l in c.state["cart"]] == [3, 5]


def test_quantity_must_be_a_number_in_range():
    c = Chat()
    c.send(tap("i:1"))
    c.send(typed("lots"))
    assert "Type a number" in c.body() and c.state["step"] == "qty"
    c.send(typed("0"))
    assert "between 1 and 50" in c.body()
    c.send(typed("51"))
    assert "between 1 and 50" in c.body()
    assert not c.state.get("cart")


def test_view_and_change_cart():
    c = Chat()
    add(c, 1, 2)
    add(c, 2, 1)
    c.send(typed("cart"))
    assert "2 × Sugar 1kg: KES 300" in c.body() and "Total: KES 380" in c.body()
    c.send(tap("k:edit"))
    assert c.ids() == ["r:0", "r:1", "k:empty"]
    c.send(tap("r:0"))
    assert "Removed Sugar 1kg" in c.body() and "Total: KES 80" in c.body()
    c.send(tap("k:empty"))
    assert "Your cart is empty" in c.body() and c.state["cart"] == []


def test_cart_drops_items_that_sold_out_and_reprices():
    c = Chat()
    add(c, 1, 2)
    add(c, 2, 1)
    c.ctx = ctx(items=[dict(ITEMS[0], price=200.0)])      # soap sold out, sugar dearer
    c.send(tap("k:view"))
    assert "removed: Bar soap" in c.body() and "Total: KES 400" in c.body()


# --- checkout ---------------------------------------------------------------------------

def checkout_to_confirm(c):
    add(c, 1, 2)
    c.send(tap("k:checkout"))


def test_checkout_asks_name_fulfilment_and_address_then_confirms():
    c = Chat()
    checkout_to_confirm(c)
    assert "What name" in c.body() and c.ids() == ["n:profile"]
    c.send(typed("Achieng Otieno"))
    assert c.ids() == ["f:pickup", "f:delivery"]
    c.send(tap("f:delivery"))
    assert "Where should we deliver" in c.body()
    c.send(bot.Event("location", address="Yaya Centre, Argwings Kodhek Rd"))
    body = c.body()
    assert "Delivery to: Yaya Centre" in body and "Total: KES 300" in body
    assert "0712 345 678" in body and c.ids() == ["y:self", "y:other", "y:cancel"]
    c.send(tap("y:self"))
    assert c.last.messages == []
    kind, details = c.last.action
    assert kind == "checkout"
    assert details == {"items": [{"id": 1, "variant_id": None, "quantity": 2}],
                       "name": "Achieng Otieno", "phone": "0712 345 678",
                       "pay_phone": "0712 345 678", "fulfilment": "delivery",
                       "address": "Yaya Centre, Argwings Kodhek Rd"}


def test_known_customer_and_pickup_only_shop_go_straight_to_confirm():
    c = Chat(known_name="Achieng O.", delivery=False)
    checkout_to_confirm(c)
    assert "Pickup at the shop" in c.body() and c.state["step"] == "confirm"


def test_profile_name_button():
    c = Chat(delivery=False)
    checkout_to_confirm(c)
    c.send(tap("n:profile"))
    assert c.state["name"] == "Achieng" and c.state["step"] == "confirm"


def test_pay_from_another_number_is_checked():
    c = Chat(known_name="A", delivery=False)
    checkout_to_confirm(c)
    c.send(tap("y:other"))
    c.send(typed("12345"))
    assert "not a Kenyan mobile number" in c.body()
    c.send(typed("0722 000 111"))
    assert "Pay with M-Pesa from 0722000111" in c.body()
    c.send(tap("y:self"))
    assert c.last.action[1]["pay_phone"] == "0722000111"
    assert c.last.action[1]["phone"] == "0712 345 678"


def test_a_number_from_abroad_must_give_a_kenyan_mpesa_number():
    c = Chat(phone="", known_name="Tom", delivery=False)
    checkout_to_confirm(c)
    assert "Kenyan M-Pesa number" in c.body() and c.state["step"] == "pay_phone"
    c.send(typed("0712345678"))
    c.send(tap("y:self"))
    assert c.last.action[1]["phone"] == "0712345678"


def test_no_mpesa_no_checkout():
    c = Chat(mpesa_online=False)
    checkout_to_confirm(c)
    assert "cannot take M-Pesa" in c.body() and c.last.action is None


def test_cancel_keeps_the_cart():
    c = Chat()
    checkout_to_confirm(c)
    c.send(typed("cancel"))
    assert "Your cart is kept" in c.body() and c.state["cart"][0]["qty"] == 2


def test_order_placed_clears_the_cart_and_waits_for_payment():
    c = Chat(known_name="A", delivery=False)
    checkout_to_confirm(c)
    c.send(tap("y:self"))
    r = bot.order_placed(c.state, c.ctx, "ORD000007", Decimal("300"), "https://x/o/pay/t")
    assert "Order ORD000007 is placed" in readable(r.messages[0])
    assert "Check 0712 345 678 for the M-Pesa prompt" in readable(r.messages[0])
    assert "cart" not in r.state and r.state["awaiting"] == ["ORD000007"]
    failed = bot.order_placed(c.state, c.ctx, "ORD000008", Decimal("300"), "https://x/p",
                              "M-Pesa did not answer.")
    assert "Pay here instead: https://x/p" in readable(failed.messages[0])


# --- other routes -----------------------------------------------------------------------

@pytest.mark.parametrize("words, action", [("orders", "orders"), ("My orders", "orders"),
                                           ("talk", "handoff"), ("agent", "handoff")])
def test_words_for_orders_and_a_person(words, action):
    c = Chat()
    c.send(typed("hi"))
    c.send(typed(words))
    assert c.last.action == (action,)


def test_my_orders_lists_pay_links_for_unpaid():
    r = bot.my_orders({}, ctx(), [
        {"number": "ORD2", "total": Decimal("300"), "status": "Waiting for payment",
         "pay_url": "https://x/pay"},
        {"number": "ORD1", "total": Decimal("80"), "status": "Collected. Thank you!",
         "pay_url": None}])
    body = readable(r.messages[0])
    assert "ORD2: KES 300, Waiting for payment" in body and "Pay: https://x/pay" in body
    assert body.count("Pay:") == 1


def test_photos_and_voice_notes_get_a_polite_answer():
    c = Chat()
    c.send(bot.Event("other"))
    assert "text and taps only" in c.body()


def test_pharmacy_says_prescription_medicines_are_at_the_counter():
    c = Chat(is_pharmacy=True)
    c.send(tap("m:shop"))
    assert "Prescription medicines are sold at the counter" in c.body()


def test_search_matches_all_words_then_any():
    assert [i["id"] for i in bot.search(ITEMS, "brown sugar")] == [4]
    assert {i["id"] for i in bot.search(ITEMS, "sugar soap")} == {1, 2, 4}
    assert bot.search(ITEMS, "a") == []
