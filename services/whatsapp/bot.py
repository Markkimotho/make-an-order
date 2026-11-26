"""The WhatsApp shop conversation, as a pure state machine.

``step(state, event, ctx)`` returns a ``Reply``: the messages to send, the
new state and, when the database has to do something (place the order, look
up orders, hand the chat to a person), an ``action`` for service.py to run.
No database, no HTTP, no clock: every conversation is a plain test.

What the bot can offer comes only from ``ctx.items``, the online shop's own
catalogue (services/online_shop.catalogue): in-stock items, server prices,
never prescription-only or controlled medicines. Anything the customer sends
(typed text, or the id of a button they tapped, which a modified client can
fake) is checked against that list; nothing in a message is trusted.

Interactive ids (at most 200 characters, see client.py):
    m:shop  m:orders  m:talk  m:menu   main menu
    cp:<n>                             category page n
    c:<category>                       a category's items
    ip:<n>                             next page of the current item list
    i:<item id>                        an item
    s:<item id>:<size id>              a size of an item
    q:<n>                              quantity
    k:checkout  k:view  k:more  k:edit  k:empty   cart
    r:<line index>                     remove a cart line
    n:profile                          use the WhatsApp profile name
    f:pickup  f:delivery               fulfilment
    y:self  y:other  y:cancel          who pays
"""
from dataclasses import dataclass, field
from decimal import Decimal

MAX_QTY = 50          # tighter than the web shop (services/online_shop.py)
MAX_LINES = 20        # tighter than the web shop
PAGE = 9              # list rows per page; the tenth is "More"
QUICK_QTYS = (1, 2, 3)

GREETINGS = {"hi", "hello", "hey", "menu", "start", "habari", "mambo", "niaje", "sasa",
             "hallo", "helo", "0", "home", "back", "main menu", "jambo", "hi there"}
WORDS = {
    "orders": "m:orders", "my orders": "m:orders", "order status": "m:orders",
    "status": "m:orders", "track": "m:orders",
    "cart": "k:view", "basket": "k:view", "view cart": "k:view",
    "checkout": "k:checkout", "check out": "k:checkout", "pay": "k:checkout",
    "shop": "m:shop", "buy": "m:shop", "order": "m:shop", "products": "m:shop",
    "catalogue": "m:shop", "catalog": "m:shop", "price list": "m:shop", "bei": "m:shop",
    "talk": "m:talk", "agent": "m:talk", "person": "m:talk", "human": "m:talk",
    "help": "m:talk", "call me": "m:talk", "msaada": "m:talk",
}
CANCEL_WORDS = {"cancel", "stop", "ghairi"}
TEXT_STEPS = ("qty", "name", "address", "pay_phone")


@dataclass
class Event:
    kind: str                 # text | choice | location | media | other
    text: str = ""            # typed text, or the tapped option's title
    choice: str = ""          # the tapped option's id
    address: str = ""         # a shared location, as words
    what: str = ""            # media: "photo", "document", "voice note", "video"


@dataclass
class Ctx:
    business: str
    items: list               # online_shop.catalogue()
    shop_open: bool = True
    delivery: bool = True
    mpesa_online: bool = True
    phone: str = ""           # the customer's number, 07XX XXX XXX
    known_name: str = ""      # the customer record's name, if any
    profile_name: str = ""    # the WhatsApp profile name
    is_pharmacy: bool = False


@dataclass
class Reply:
    messages: list
    state: dict
    action: tuple | None = None      # ("checkout", details) | ("orders",) | ("handoff",)


# --- message builders -----------------------------------------------------------------

def say(body):
    return {"type": "text", "body": body}


def buttons(body, *pairs):
    return {"type": "buttons", "body": body, "buttons": list(pairs)}


def options(body, rows, button="Choose", section="Options", header=None):
    return {"type": "list", "body": body, "rows": rows, "button": button,
            "section": section, "header": header}


def kes(amount):
    amount = Decimal(str(amount))
    return f"KES {amount:,.0f}" if amount == amount.to_integral() else f"KES {amount:,.2f}"


# --- the catalogue --------------------------------------------------------------------

def _by_id(ctx):
    return {item["id"]: item for item in ctx.items}


def _categories(ctx):
    counts = {}
    for item in ctx.items:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[0].lower())


def _price_of(item, size_id):
    if size_id is None:
        return Decimal(str(item["price"])), None
    for size in item.get("sizes") or []:
        if size["id"] == size_id:
            return Decimal(str(size["price"])), size
    return None, None


def _stock_note(stock):
    return "few left" if stock == "few" else "in stock"


def search(items, query):
    """Items whose name has every word of the query; failing that, any word.
    Case-insensitive; words under 2 letters are ignored."""
    words = [w for w in query.lower().replace(",", " ").split() if len(w) >= 2]
    if not words:
        return []
    def hay(item):
        return f"{item['name']} {item.get('description', '')} {item['category']}".lower()
    every = [i for i in items if all(w in hay(i) for w in words)]
    if every:
        return every
    return [i for i in items if any(w in hay(i) for w in words)]


# --- the cart -------------------------------------------------------------------------

def cart_lines(state, ctx):
    """[(line, item, size, unit price)] for cart lines still sold, and the
    names of lines that are not (sold out or no longer online)."""
    found, gone = [], []
    by_id = _by_id(ctx)
    for line in state.get("cart") or []:
        item = by_id.get(line.get("id"))
        price, size = _price_of(item, line.get("size")) if item else (None, None)
        if item is None or price is None:
            gone.append(line.get("name") or "an item")
        else:
            found.append((line, item, size, price))
    return found, gone


def _label(item, size):
    return item["name"] + (f" ({size['label']})" if size else "")


def cart_total(state, ctx):
    found, _ = cart_lines(state, ctx)
    return sum((price * line["qty"] for line, _, _, price in found), Decimal("0"))


def _cart_count(state):
    return sum(int(line.get("qty") or 0) for line in state.get("cart") or [])


def _add(state, item, size, qty):
    cart = [dict(line) for line in state.get("cart") or []]
    size_id = size["id"] if size else None
    for line in cart:
        if line["id"] == item["id"] and line.get("size") == size_id:
            line["qty"] = min(MAX_QTY, line["qty"] + qty)
            break
    else:
        if len(cart) >= MAX_LINES:
            return None
        cart.append({"id": item["id"], "size": size_id, "qty": qty,
                     "name": _label(item, size)})
    return cart


# --- screens --------------------------------------------------------------------------

def menu(state, ctx, intro=None):
    head = intro or f"Welcome to {ctx.business}. What would you like to do?"
    if not ctx.shop_open:
        return Reply([buttons(f"{head}\n\nWe are not taking orders on WhatsApp right now.",
                              ("m:orders", "My orders"), ("m:talk", "Talk to us"))],
                     {**state, "step": "menu"})
    count = _cart_count(state)
    if count:
        body = (f"{head}\n\nYou have {count} item{'s' if count != 1 else ''} in your cart. "
                "Type ORDERS to see your orders.")
        pairs = (("m:shop", "Shop"), ("k:view", "View cart"), ("m:talk", "Talk to us"))
    else:
        body = f"{head}\n\nOr just type what you are looking for."
        pairs = (("m:shop", "Shop"), ("m:orders", "My orders"), ("m:talk", "Talk to us"))
    return Reply([buttons(body, *pairs)], {**state, "step": "menu"})


def categories(state, ctx, page=0):
    cats = _categories(ctx)
    if not cats:
        return Reply([buttons("Nothing is available to order online right now.",
                              ("m:talk", "Talk to us"))], {**state, "step": "menu"})
    if len(cats) == 1:
        return item_list({**state, "browse": {"cat": cats[0][0]}}, ctx, 0)
    start = page * PAGE
    chunk = cats[start:start + PAGE]
    rows = [(f"c:{name}", name, f"{n} item{'s' if n != 1 else ''}") for name, n in chunk]
    if start + PAGE < len(cats):
        rows.append((f"cp:{page + 1}", "More categories", ""))
    body = "Pick a category, or type what you are looking for (for example: sugar)."
    if ctx.is_pharmacy:
        body += "\nPrescription medicines are sold at the counter, not here."
    return Reply([options(body, rows, button="Categories", section="Categories")],
                 {**state, "step": "browse"})


def _browse_items(state, ctx):
    browse = state.get("browse") or {}
    if "cat" in browse:
        return [i for i in ctx.items if i["category"] == browse["cat"]], browse["cat"]
    if "q" in browse:
        return search(ctx.items, browse["q"]), f"\"{browse['q']}\""
    return [], ""


def _item_row(item):
    price = f"from {kes(item['price'])}" if item.get("sizes") else kes(item["price"])
    return (f"i:{item['id']}", item["name"], f"{price} · {_stock_note(item.get('stock'))}")


def item_list(state, ctx, page=0):
    items, title = _browse_items(state, ctx)
    if not items:
        return categories(state, ctx)
    start = page * PAGE
    chunk = items[start:start + PAGE]
    if not chunk:
        return item_list(state, ctx, 0)
    rows = [_item_row(i) for i in chunk]
    if start + PAGE < len(items):
        rows.append((f"ip:{page + 1}", "More items", f"{len(items) - start - PAGE} more"))
    shown = f"{start + 1}-{start + len(chunk)} of {len(items)}" if len(items) > PAGE else ""
    body = f"{title}: pick an item." + (f" ({shown})" if shown else "")
    return Reply([options(body, rows, button="Items", section="Items")],
                 {**state, "step": "browse", "browse": {**(state.get("browse") or {}),
                                                        "page": page}})


def show_item(state, ctx, item):
    sizes = item.get("sizes") or []
    if sizes and len(sizes) > 1:
        rows = [(f"s:{item['id']}:{s['id']}", s["label"],
                 f"{kes(s['price'])} · {_stock_note(s.get('stock'))}") for s in sizes[:10]]
        return Reply([options(f"{item['name']}: pick a size.", rows, button="Sizes",
                              section="Sizes")], {**state, "step": "browse"})
    size = sizes[0] if sizes else None
    return ask_qty(state, item, size)


def ask_qty(state, item, size):
    price = Decimal(str(size["price"] if size else item["price"]))
    desc = f"\n{item['description']}" if item.get("description") else ""
    body = (f"{_label(item, size)}: {kes(price)}{' per ' + item['unit'] if item.get('unit') else ''}"
            f"{desc}\n\nHow many? Tap or type a number.")
    return Reply([buttons(body, *[(f"q:{n}", str(n)) for n in QUICK_QTYS])],
                 {**state, "step": "qty",
                  "pending": {"id": item["id"], "size": size["id"] if size else None}})


def view_cart(state, ctx, note=""):
    found, gone = cart_lines(state, ctx)
    cart = [line for line, _, _, _ in found]
    state = {**state, "cart": cart}
    lines = [note] if note else []
    if gone:
        lines.append("No longer available, so removed: " + ", ".join(gone) + ".")
    if not found:
        lines.append("Your cart is empty.")
        return Reply([buttons("\n".join(lines), ("m:shop", "Shop"), ("m:orders", "My orders"))],
                     {**state, "step": "menu"})
    lines.append("Your cart:")
    for line, item, size, price in found:
        lines.append(f"• {line['qty']} × {_label(item, size)}: {kes(price * line['qty'])}")
    lines.append(f"Total: {kes(cart_total(state, ctx))}")
    return Reply([buttons("\n".join(lines), ("k:checkout", "Checkout"),
                          ("k:more", "Keep shopping"), ("k:edit", "Change cart"))],
                 {**state, "step": "cart"})


def edit_cart(state, ctx):
    found, _ = cart_lines(state, ctx)
    if not found:
        return view_cart(state, ctx)
    rows = [(f"r:{n}", f"Remove {_label(item, size)}", f"{line['qty']} × {kes(price)}")
            for n, (line, item, size, price) in enumerate(found[:9])]
    rows.append(("k:empty", "Empty the cart", ""))
    return Reply([options("Tap an item to take it out of the cart.", rows, button="Change",
                          section="Cart")],
                 {**state, "step": "cart", "cart": [l for l, _, _, _ in found]})


def my_orders(state, ctx, rows):
    """``rows``: [{"number", "total", "status", "pay_url" or None}], newest first."""
    if not rows:
        return Reply([buttons("You have no orders with us yet.", ("m:shop", "Shop"),
                              ("m:talk", "Talk to us"))], {**state, "step": "menu"})
    lines = ["Your recent orders:"]
    for row in rows:
        lines.append(f"• {row['number']}: {kes(row['total'])}, {row['status']}")
        if row.get("pay_url"):
            lines.append(f"  Pay: {row['pay_url']}")
    return Reply([buttons("\n".join(lines), ("m:shop", "Shop"), ("m:talk", "Talk to us"))],
                 {**state, "step": "menu"})


def handed_off(state, ctx, what=None):
    if what:
        body = (f"Thanks, we got your {what}. Someone from {ctx.business} will look at it "
                "and reply here soon. Type MENU any time to go back to the shop.")
    else:
        body = (f"OK. Someone from {ctx.business} will reply here soon. "
                "Type MENU any time to go back to the shop.")
    return Reply([say(body)], {**state, "step": "menu"})


# --- checkout -------------------------------------------------------------------------

def checkout(state, ctx):
    if not ctx.shop_open:
        return menu(state, ctx)
    if not ctx.mpesa_online:
        return Reply([buttons(f"{ctx.business} cannot take M-Pesa payments here yet. "
                              "Order at the counter, or talk to us.",
                              ("m:talk", "Talk to us"), ("m:menu", "Menu"))],
                     {**state, "step": "menu"})
    found, gone = cart_lines(state, ctx)
    if gone or not found:
        return view_cart(state, ctx)
    if not (state.get("name") or ctx.known_name):
        return ask_name(state, ctx)
    if ctx.delivery and not state.get("fulfilment"):
        return Reply([buttons("Pickup or delivery?", ("f:pickup", "I'll pick it up"),
                              ("f:delivery", "Deliver to me"))],
                     {**state, "step": "fulfil"})
    if state.get("fulfilment") == "delivery" and not state.get("address"):
        return Reply([say("Where should we deliver? Type the area and street or a "
                           "landmark, or share your location.")],
                     {**state, "step": "address"})
    return confirm(state, ctx)


def ask_name(state, ctx):
    profile = (ctx.profile_name or "").strip()
    body = "What name should we put on the order? Type it below."
    if 2 <= len(profile) <= 40:
        return Reply([buttons(body, ("n:profile", f"Use {profile}"[:20]))],
                     {**state, "step": "name"})
    return Reply([say(body)], {**state, "step": "name"})


def confirm(state, ctx):
    found, _ = cart_lines(state, ctx)
    fulfilment = state.get("fulfilment") if ctx.delivery else "pickup"
    fulfilment = fulfilment or "pickup"
    where = (f"Delivery to: {state.get('address')}" if fulfilment == "delivery"
             else "Pickup at the shop")
    lines = [f"{line['qty']} × {_label(item, size)}: {kes(price * line['qty'])}"
             for line, item, size, price in found]
    pay_phone = state.get("pay_phone") or ctx.phone
    if not pay_phone:
        # A number from outside Kenya: M-Pesa needs a Kenyan one.
        return Reply([say("Type the Kenyan M-Pesa number to pay from, like 0712 345 678.")],
                     {**state, "step": "pay_phone", "fulfilment": fulfilment})
    body = ("\n".join(lines) + f"\nTotal: {kes(cart_total(state, ctx))}\n{where}\n\n"
            f"Pay with M-Pesa from {pay_phone}?")
    return Reply([buttons(body, ("y:self", "Yes, send prompt"), ("y:other", "Another number"),
                          ("y:cancel", "Not now"))],
                 {**state, "step": "confirm", "fulfilment": fulfilment})


def place(state, ctx):
    """Everything the order needs, for service.py to place it."""
    found, _ = cart_lines(state, ctx)
    details = {"items": [{"id": line["id"], "variant_id": line.get("size"),
                          "quantity": line["qty"]} for line, _, _, _ in found],
               "name": state.get("name") or ctx.known_name,
               "phone": ctx.phone or state.get("pay_phone"),
               "pay_phone": state.get("pay_phone") or ctx.phone,
               "fulfilment": state.get("fulfilment") or "pickup",
               "address": state.get("address")}
    return Reply([], {**state, "step": "placing"}, ("checkout", details))


def order_placed(state, ctx, number, total, pay_url, prompt_error=None):
    """After service.py placed the order (and tried the M-Pesa prompt)."""
    new = {k: v for k, v in state.items()
           if k not in ("cart", "pending", "browse", "address", "pay_phone", "fulfilment")}
    new["step"] = "menu"
    new["awaiting"] = (list(state.get("awaiting") or []) + [number])[-5:]
    pay_phone = state.get("pay_phone") or ctx.phone
    if prompt_error:
        body = (f"Order {number} is placed. Total {kes(total)}.\n"
                f"{prompt_error}\nPay here instead: {pay_url}")
    else:
        body = (f"Order {number} is placed. Total {kes(total)}.\n"
                f"Check {pay_phone} for the M-Pesa prompt and enter your PIN. "
                f"No prompt? Pay here: {pay_url}")
    body += "\nWe hold your items for 15 minutes while you pay."
    return Reply([say(body)], new)


def order_failed(state, ctx, error):
    return Reply([buttons(f"Your order could not be placed: {error}",
                          ("k:view", "View cart"), ("m:talk", "Talk to us"))],
                 {**state, "step": "cart"})


def paid_notice(number, fulfilment):
    then = ("We will tell you when it is on its way." if fulfilment == "delivery"
            else "We will tell you when it is ready to pick up.")
    return say(f"Payment received for order {number}. Thank you! {then}")


# --- the dispatcher -------------------------------------------------------------------

def _number(raw):
    raw = raw.strip()
    return int(raw) if raw.isdigit() and len(raw) <= 3 else None


def step(state, event, ctx):
    """One customer message in; the bot's answer out."""
    state = dict(state or {})
    first = not state.get("seen")
    state["seen"] = True
    if event.kind == "media":
        # A photo (often a prescription or a picture of what they want), a
        # document or a voice note: a person has to look at it.
        return Reply([], state, ("handoff", event.what or "file"))
    if event.kind == "other":
        return Reply([buttons("I can read text and taps only. Type what you are looking "
                              "for, or talk to us.", ("m:shop", "Shop"),
                              ("m:talk", "Talk to us"))], state)

    choice = event.choice if event.kind == "choice" else ""
    typed = event.text.strip() if event.kind == "text" else ""
    low = " ".join(typed.lower().split()).strip(".!?")

    if event.kind == "location":
        if state.get("step") == "address" and event.address:
            return checkout({**state, "address": event.address[:255]}, ctx)
        return Reply([say("Thanks. Share your location when we ask where to deliver.")],
                     state)

    # Words that work anywhere.
    if typed:
        if low in GREETINGS and not (low == "0" and state.get("step") == "qty"):
            return menu(state, ctx)
        if low in CANCEL_WORDS:
            return menu({**state, "pending": None}, ctx, "OK, stopped. Your cart is kept.")
        if state.get("step") not in TEXT_STEPS and low in WORDS:
            choice = WORDS[low]

    # Steps that wait for typed text.
    if not choice and state.get("step") == "qty":
        qty = _number(typed)
        if qty is None:
            return Reply([say("Type a number, like 2. Or type MENU.")], state)
        choice = f"q:{qty}"
    if not choice and state.get("step") == "name":
        name = " ".join(typed.split())[:80]
        if len(name) < 2:
            return Reply([say("Type your name, at least 2 letters.")], state)
        return checkout({**state, "name": name}, ctx)
    if not choice and state.get("step") == "address":
        address = " ".join(typed.split())[:255]
        if len(address) < 3:
            return Reply([say("Type where to deliver: the area and street, or a landmark.")],
                         state)
        return checkout({**state, "address": address}, ctx)
    if not choice and state.get("step") == "pay_phone":
        from services.phone import InvalidPhone, normalize_ke_phone
        try:
            phone = normalize_ke_phone(typed)
        except InvalidPhone:
            return Reply([say("That is not a Kenyan mobile number. Type one like "
                               "0712 345 678.")], state)
        return confirm({**state, "pay_phone": "0" + phone[3:]}, ctx)

    if not choice:
        if not typed or not ctx.shop_open:
            return menu(state, ctx)
        found = search(ctx.items, typed)
        if not found and first:
            # "Hello, I need help": greet rather than say nothing matches.
            return menu(state, ctx)
        if not found:
            return Reply([buttons(f"Nothing matches \"{typed[:40]}\". Try another word, "
                                  "or browse the shop.", ("m:shop", "Browse the shop"),
                                  ("m:talk", "Talk to us"))], {**state, "step": "menu"})
        if len(found) == 1:
            return show_item(state, ctx, found[0])
        return item_list({**state, "browse": {"q": typed[:40]}}, ctx, 0)

    return _choose(state, choice, ctx)


def _choose(state, choice, ctx):
    kind, _, arg = choice.partition(":")
    by_id = _by_id(ctx)

    if choice == "m:menu":
        return menu(state, ctx)
    if choice == "m:shop" or choice == "k:more":
        return categories(state, ctx) if ctx.shop_open else menu(state, ctx)
    if choice == "m:orders":
        return Reply([], state, ("orders",))
    if choice == "m:talk":
        return Reply([], state, ("handoff",))
    if not ctx.shop_open and kind in ("cp", "c", "ip", "i", "s", "q", "k", "f", "y", "n"):
        return menu(state, ctx)
    if kind == "cp" and arg.isdigit():
        return categories(state, ctx, int(arg))
    if kind == "c":
        if not any(cat == arg for cat, _ in _categories(ctx)):
            return categories(state, ctx)
        return item_list({**state, "browse": {"cat": arg}}, ctx, 0)
    if kind == "ip" and arg.isdigit():
        return item_list(state, ctx, int(arg))
    if kind == "i" and arg.isdigit():
        item = by_id.get(int(arg))
        if item is None:
            return Reply([buttons("Sorry, that item is not available any more.",
                                  ("m:shop", "Shop"), ("k:view", "View cart"))], state)
        return show_item(state, ctx, item)
    if kind == "s":
        item_id, _, size_id = arg.partition(":")
        item = by_id.get(int(item_id)) if item_id.isdigit() else None
        _, size = _price_of(item, int(size_id)) if item and size_id.isdigit() else (None, None)
        if size is None:
            return Reply([buttons("Sorry, that size is not available any more.",
                                  ("m:shop", "Shop"))], state)
        return ask_qty(state, item, size)
    if kind == "q" and arg.isdigit():
        pending = state.get("pending") or {}
        item = by_id.get(pending.get("id"))
        _, size = _price_of(item, pending.get("size")) if item else (None, None)
        if item is None or (pending.get("size") is not None and size is None):
            return Reply([buttons("Pick an item first.", ("m:shop", "Shop"))],
                         {**state, "step": "menu", "pending": None})
        qty = int(arg)
        if not 1 <= qty <= MAX_QTY:
            return Reply([say(f"Choose between 1 and {MAX_QTY}.")], state)
        cart = _add(state, item, size, qty)
        if cart is None:
            return view_cart(state, ctx, f"Your cart is full ({MAX_LINES} items). "
                                         "Check out first.")
        state = {**state, "cart": cart, "pending": None, "step": "cart"}
        count, total = _cart_count(state), cart_total(state, ctx)
        return Reply([buttons(f"Added {qty} × {_label(item, size)}.\n"
                              f"Cart: {count} item{'s' if count != 1 else ''}, {kes(total)}.",
                              ("k:checkout", "Checkout"), ("k:more", "Keep shopping"),
                              ("k:view", "View cart"))], state)
    if choice == "k:view":
        return view_cart(state, ctx)
    if choice == "k:edit":
        return edit_cart(state, ctx)
    if choice == "k:empty":
        return view_cart({**state, "cart": []}, ctx, "Cart emptied.")
    if kind == "r" and arg.isdigit():
        found, _ = cart_lines(state, ctx)
        lines = [line for line, _, _, _ in found]
        n = int(arg)
        if n < len(lines):
            removed = lines.pop(n)
            return view_cart({**state, "cart": lines}, ctx, f"Removed {removed.get('name')}.")
        return view_cart(state, ctx)
    if choice == "k:checkout":
        return checkout({**state, "fulfilment": None, "address": None, "pay_phone": None}, ctx)
    if choice == "n:profile" and state.get("step") == "name":
        name = " ".join((ctx.profile_name or "").split())[:80]
        if len(name) >= 2:
            return checkout({**state, "name": name}, ctx)
        return ask_name(state, ctx)
    if kind == "f" and arg in ("pickup", "delivery") and state.get("step") == "fulfil":
        if arg == "delivery" and not ctx.delivery:
            arg = "pickup"
        return checkout({**state, "fulfilment": arg,
                         "address": None if arg == "pickup" else state.get("address")}, ctx)
    if choice == "y:self" and state.get("step") == "confirm":
        return place(state, ctx)
    if choice == "y:other" and state.get("step") == "confirm":
        return Reply([say("Type the M-Pesa number to send the prompt to, like 0712 345 678.")],
                     {**state, "step": "pay_phone"})
    if choice == "y:cancel":
        return menu(state, ctx, "OK, not now. Your cart is kept; type CHECKOUT when ready.")
    # An old button from earlier in the chat, or a step that has moved on.
    return menu(state, ctx)
