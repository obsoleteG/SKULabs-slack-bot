import os
import json
import re
import logging
import requests
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from SKULabs import SKULabsClient

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hold_bot")

app = App(token=os.environ["SLACK_BOT_TOKEN"])
skulabs = SKULabsClient(os.environ["SKULABS_API_KEY"])

RAINSIS_URL = os.environ.get("RAINSIS_URL", "http://localhost:8000")
HOLD_BOT_SECRET = os.environ.get("HOLD_BOT_SECRET", "")
_raw_channels = os.environ.get("SUPPORT_CHANNEL_ID", "")
SUPPORT_CHANNEL_IDS: set[str] = {c.strip() for c in _raw_channels.split(",") if c.strip()}

# Pattern: #12345 HOLD  or  EXC-72329-1-1 HOLD  or  #EXC-72329-1-1 HOLD
_HOLD_PATTERN = re.compile(r"(#\d+|#?[A-Z]{2,}-[\d][\d\-]*)\s+HOLD\b", re.IGNORECASE)


@app.message(_HOLD_PATTERN)
def handle_hold_message(message, say, client):
    """Detect '#XXXXX HOLD' messages and register the hold in RainSis."""
    channel = message.get("channel", "")
    ts = message.get("ts", "")
    user = message.get("user", "")
    text = message.get("text", "")

    # Optionally restrict to configured channels
    if SUPPORT_CHANNEL_IDS and channel not in SUPPORT_CHANNEL_IDS:
        return

    matches = _HOLD_PATTERN.findall(text)
    for order_number in matches:
        order_number = order_number.lstrip("#")
        logger.info("HOLD detected: order #%s in channel %s", order_number, channel)
        try:
            resp = requests.post(
                f"{RAINSIS_URL}/api/warehouse/hold/register",
                json={
                    "order_number": order_number,
                    "slack_channel": channel,
                    "slack_user": user,
                    "slack_message_ts": ts,
                },
                headers={"X-Bot-Token": HOLD_BOT_SECRET},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            found_in = data.get("found_in", "unknown")
            action = data.get("action_taken", "none")
            batch_label = data.get("batch_label", "")
            fulfilled = data.get("shopify_fulfilled", False)
            label = data.get("label_printed", False)
            duplicate = data.get("duplicate", False)
            created_at = data.get("created_at", "")[:16].replace("T", " ") if data.get("created_at") else ""

            # Duplicate — already on HOLD
            if duplicate:
                msg = f"Tika pievienots HOLD sarakstam {created_at}. :white_check_mark:"
                client.chat_postMessage(channel=channel, thread_ts=ts, text=msg)
                continue

            # Build a reply for the Slack thread
            if found_in == "active_picklist":
                if fulfilled:
                    msg = f"Fulfillots. Pievienots HOLD sarakstam. :white_check_mark: (Picklist {batch_label})"
                elif label:
                    msg = f"Labeli izprintēti, vēl pikojas. Pievienots HOLD sarakstam. :white_check_mark: (Picklist {batch_label})"
                else:
                    msg = f"Pikojas, nav labeli izdrukāti. Pievienots HOLD sarakstam. :white_check_mark: (Picklist {batch_label})"
            elif found_in == "finished_picklist":
                msg = f"Sapikots. Jāmekle pie izsūtāmajiem :rotating_light: (Picklist {batch_label})"
            elif found_in == "shopify_only":
                msg = f"Orderi redzu tikai Shopify. Pievienoju HOLD sarakstam. :white_check_mark:"
            elif found_in == "order_pool":
                msg = f"Pievienots HOLD sarakstam. :white_check_mark:"
            else:
                msg = f"Nekur neatrodu tādu orderi. :question:"

            # Reply in thread
            logger.info("HOLD sending reply for #%s: %r", order_number, msg)
            client.chat_postMessage(channel=channel, thread_ts=ts, text=msg)
            logger.info("HOLD registered for #%s: found_in=%s action=%s", order_number, found_in, action)
        except Exception as exc:
            logger.exception("Failed to register HOLD for #%s: %s", order_number, exc)
            client.chat_postMessage(
                channel=channel, thread_ts=ts,
                text=f"Neizdevās apstrādāt HOLD #{order_number}. :x: Kļūda: {exc}"
            )


@app.event("message")
def handle_unmatched_messages(event, logger):
    """Catch-all to silence 'Unhandled request' warnings for non-HOLD messages."""
    subtype = event.get("subtype")
    text = event.get("text", "")
    if subtype is None and _HOLD_PATTERN.search(text):
        logger.warning("HOLD pattern seen in catch-all but missed by @app.message — text: %r", text)


@app.command("/sku")
def handle_sku(ack, respond, command):
    ack()
    sku = command.get("text", "").strip()
    if not sku:
        respond("Please provide a SKU. Usage: `/sku MIBKS`")
        return
    try:
        data = skulabs.get_item_by_sku(sku)
        if not data:
            respond(f"No item found for SKU `{sku}`.")
            return

        item_id = data["item_id"]
        locations = data["locations"]
        total = sum(loc["items"][0]["on_hand"] for loc in locations if loc["items"])

        lines = [f"*{data['name']}*", f"SKU: `{data['sku']}`", ""]
        # Stock per location
        for loc in locations:
            qty = loc["items"][0]["on_hand"] if loc["items"] else 0
            if qty > 0:
                lines.append(f"• {loc['name']}: *{qty}*")

        lines.append(f"\n*Total in stock: {total}*")

        # Reserved
        open_orders = skulabs.get_open_orders_for_item(data["sku"])
        reserved = sum(o["qty"] for o in open_orders)
        if reserved > 0:
            lines.append(f"🔒 *Reserved for open orders: {reserved}*")
            for o in open_orders:
                lines.append(f"  • Order #{o['order_number']} — {o['qty']} unit(s)")

        # Purchase Orders
        pos = skulabs.get_purchase_orders_for_item(item_id)
        if pos:
            lines.append("\n*Open Purchase Orders:*")
            for po in pos:
                raw_date = po["expected_date"] or ""
                date_str = raw_date[:10] if raw_date else "No date set"
                lines.append(f"• PO #{po['po_number']} — *{po['incoming_qty']} units* incoming, ETA: {date_str}")
        else:
            lines.append("_No open purchase orders for this item._")

        respond("\n".join(lines))

    except Exception as e:
        respond(f"Error: {e}")


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()