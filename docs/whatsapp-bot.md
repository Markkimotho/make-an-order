# WhatsApp chatbot

Customers message the business's own WhatsApp number, browse what is in
stock, order and pay by M-Pesa. Nobody at the counter has to do anything
until the order is paid. Paid orders show up under **Online orders**, the
same as orders from the web shop. A customer who taps **Talk to us** goes to
**WhatsApp chats**, where staff answer from the app.

## What the customer sees

1. They send "Hi" (or the name of a product) to the shop's number.
2. Menu buttons: **Shop**, **My orders**, **Talk to us**.
3. **Shop** lists categories, then items, with price and "in stock / few
   left". Typing a word ("sugar") searches.
4. They pick an item, a size if it has sizes, and a quantity. The cart keeps a
   running total. **Change cart** removes lines.
5. **Checkout** asks for their name (once; a known customer is not asked),
   pickup or delivery, and the delivery address (typed or a shared
   location).
6. They confirm. The order is placed and the stock is held for 15 minutes.
   An M-Pesa prompt goes to their phone, and a pay link is sent in case the
   prompt does not arrive.
7. When the payment lands they get "Payment received". When staff mark the
   order ready or out for delivery they get a WhatsApp message, or an SMS
   if they last wrote more than 24 hours ago.

A customer who sends a **photo, document, voice note or video** (a
prescription, a picture of what they want) is handed to a person straight
away: "Thanks, we got your photo. Someone will look at it and reply here."
Staff see it inline in **WhatsApp chats** (photos, voice notes and videos
play in the page; other files download). Files are fetched from Meta when
opened, through `/whatsapp/media/<message id>` (till staff only), and are
not copied to our server. Meta keeps them for about 30 days; files over
16 MB are opened in WhatsApp itself. Only images, PDF, audio and video open
in the browser; anything else (HTML, SVG, office files) downloads.

Words that work anywhere: `hi` / `menu` (start over), `cart`, `checkout`,
`orders`, `talk`, `cancel`.

## Rules the bot keeps

- Prices, stock and what may be sold online come from the web shop's own
  code (`services/online_shop.py`). Prescription-only and controlled
  medicines are never offered. The bot never quotes a price itself.
- Nothing a customer sends is trusted. A tapped button's id is checked
  against the live catalogue like typed text.
- One order per confirmation. Messages from one customer are handled one at
  a time (the chat row is locked), and Meta's retried webhooks are dropped
  by message id.
- WhatsApp orders are paid only by a payment made for that order, never from
  the customer's credit (`services/billing/payments.py`,
  `ANONYMOUS_CHANNELS`).
- Messages are sent only inside Meta's 24-hour window after the customer's
  last message. Outside it, order updates go by SMS and staff are told to
  call.

## Connecting a number

The business needs its own WhatsApp Business number on Meta's Cloud API.
The steps are also on the Settings page (owner only):

1. At [developers.facebook.com/apps](https://developers.facebook.com/apps),
   create a **Business** app and add **WhatsApp**.
2. Under WhatsApp > API Setup, add and verify the business number. A number
   used in the WhatsApp Business app must move to the API, or use Meta's
   coexistence option.
3. Copy the **Phone number ID** and **WhatsApp Business account ID** into
   Settings > WhatsApp chatbot.
4. In Business Settings > System users, create a system user. Assign it the
   app and the WhatsApp account, then generate a permanent token with
   `whatsapp_business_messaging` and `whatsapp_business_management`. Paste
   the token.
5. Copy the **App secret** (app Settings > Basic) and paste it. Save.
6. Under WhatsApp > Configuration, enter the webhook URL
   (`https://<your host>/webhooks/whatsapp/<slug>`) and the verify token
   shown in Settings. Verify, then subscribe to **messages**.
7. Press **Check connection**, then **Send test message** to your own
   phone (Meta's `hello_world` template; on a Meta test number, add your
   phone as a recipient first). Switch the bot on and reply "Hi".

The token and app secret are stored encrypted (`TENANT_SECRETS_KEY`) and are
never shown again. A shop server needs the Cloudflare tunnel
(`docker compose --profile tunnel up`, see [Install in the
shop](install-local.md)) so Meta can reach the webhook. M-Pesa (Daraja)
must be set up for the bot to take payments; without it the bot only offers
**Talk to us**.

## Where it lives

| Piece | File |
|---|---|
| Conversation (pure state machine) | `services/whatsapp/bot.py` |
| Webhook handling, orders, sending | `services/whatsapp/service.py` |
| Meta Cloud API, signatures | `services/whatsapp/client.py` |
| Webhook routes | `api/whatsapp_webhook.py` |
| Staff chats API and page | `api/whatsapp_chats.py`, `pages/whatsapp_chats.py`, `templates/whatsapp_chats.html` |
| Settings | `api/settings.py` (`/settings/whatsapp*`) |
| Tables | `whatsapp_chats`, `whatsapp_messages` (migrations 0015, 0016 for media) |
| Tests | `tests/test_whatsapp_bot.py` (conversations), `tests/test_whatsapp.py` (end to end) |

The bot uses menus and fixed rules, with no language model, so there is no
eval suite: every reply is covered by a gate test.

## Measuring it

- Orders from the bot: `orders.channel = 'whatsapp'` (Online orders, filter
  "From: WhatsApp").
- Conversations and every message, with delivery status:
  `whatsapp_messages`.
- Log lines: `whatsapp order <number> placed`, and
  `whatsapp webhook <slug>: {...}` per delivery.
- Payment notices sent per sweep: `whatsapp_notices` in the
  `flask payments-sweep` summary.

## Settings

| Setting | Where |
|---|---|
| `WHATSAPP_GRAPH_VERSION` | Environment. Meta Graph API version, default `v23.0`. Bump it when Meta retires a version. |
| Number id, account id, token, app secret, bot on/off | Settings > WhatsApp chatbot |
