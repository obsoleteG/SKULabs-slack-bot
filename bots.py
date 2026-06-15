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
_raw_channels = os.environ.get("CHANNEL_IDS", "") or os.environ.get("SUPPORT_CHANNEL_ID", "")
SUPPORT_CHANNEL_IDS: set[str] = {c.strip() for c in _raw_channels.split(",") if c.strip()}
HISTORY_SCAN_CHANNEL_ID = os.environ.get("HISTORY_SCAN_CHANNEL_ID", "")
RIGA_WAREHOUSE_CHANNEL_ID = os.environ.get("RIGA_WAREHOUSE_CHANNEL_ID", "C08K0GKECQ5")

# Pattern: #12345 HOLD  or  EXC-72329-1-1 HOLD  or  #EXC-72329-1-1 HOLD
_HOLD_PATTERN = re.compile(r"(#\d+|#?[A-Z]{2,}-[\d][\d\-]*)\s+HOLD\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Tracking number patterns — ordered most-specific to least-specific
# ---------------------------------------------------------------------------
_TRACKING_PATTERNS = [
    # ── Unambiguous prefix/suffix patterns ───────────────────────────────────
    ("ups",            "UPS",            re.compile(r"\b1Z[A-Z0-9]{16}\b")),
    ("venipak",        "Venipak",        re.compile(r"\bV[0-9]{5}[A-Z][0-9]{7}\b")),
    ("dhl-express",    "DHL Express",    re.compile(r"\bJD[0-9]{18}\b")),
    ("cainiao",        "Cainiao",        re.compile(r"\bLP[0-9]{12,18}\b")),
    ("cainiao",        "Cainiao",        re.compile(r"\bUR[0-9]{9}CN\b")),
    # International postal XX[0-9]{9}CC — specific country codes
    ("latvijas-pasts", "Latvijas Pasts", re.compile(r"\bC[A-Z][0-9]{9}LV\b")),
    ("latvijas-pasts", "Latvijas Pasts", re.compile(r"\b[A-Z]{2}[0-9]{9}LV\b")),
    ("omniva",         "Omniva",         re.compile(r"\b[A-Z]{2}[0-9]{9}EE\b")),
    ("itella",         "Itella",         re.compile(r"\b[A-Z]{2}[0-9]{9}FI\b")),
    ("postnl",         "PostNL",         re.compile(r"\b[A-Z]{2}[0-9]{9}NL\b")),
    ("china-post",     "China Post",     re.compile(r"\b[A-Z]{2}[0-9]{9}CN\b")),
    ("post-de",        "Deutsche Post",  re.compile(r"\b[A-Z]{2}[0-9]{9}DE\b")),
    ("usps",           "USPS",           re.compile(r"\b9[0-9]{21}\b")),
    ("nova-poshta",    "Nova Post",      re.compile(r"\b59[0-9]{12}\b")),
    ("dpd",            "DPD",            re.compile(r"\b[01][0-9]{13}\b")),
    # ── Ambiguous numeric-only (match last) ──────────────────────────────────
    ("fedex",          "FedEx",          re.compile(r"\b[0-9]{15}\b")),
    ("fedex",          "FedEx",          re.compile(r"\b[0-9]{12}\b")),
    ("dhl-express",    "DHL Express",    re.compile(r"\b[0-9]{10}\b")),
    ("gls",            "GLS",            re.compile(r"\b[0-9]{8}\b")),
]

def _extract_trackings(text: str) -> list[dict]:
    found, seen = [], set()
    for slug, name, pat in _TRACKING_PATTERNS:
        for m in pat.finditer(text):
            num = m.group(0)
            if num not in seen:
                seen.add(num)
                found.append({"tracking_number": num, "carrier_slug": slug, "carrier": name})
    return found

def _extract_receiver(text: str) -> str:
    """Return first <@UXXXXXX> mention from Slack message text."""
    m = re.search(r"<@([A-Z0-9]+)>", text)
    return m.group(1) if m else ""

def _resolve_display_name(client, user_id: str) -> str:
    if not user_id:
        return ""
    try:
        info = client.users_info(user=user_id)
        profile = info["user"]["profile"]
        return profile.get("display_name") or profile.get("real_name") or user_id
    except Exception:
        return user_id


@app.message(_HOLD_PATTERN)
def handle_hold_message(message, say, client):
    """Detect '#XXXXX HOLD' messages and register the hold in RainSis."""
    channel = message.get("channel", "")
    ts = message.get("ts", "")
    thread_ts = message.get("thread_ts", "")
    user = message.get("user", "")
    text = message.get("text", "")

    # Thread replies containing HOLD text should be handled as thread updates, not new HOLDs
    if thread_ts and thread_ts != ts:
        thread = _fetch_thread(client, channel, thread_ts)
        if SUPPORT_CHANNEL_IDS and channel in SUPPORT_CHANNEL_IDS:
            try:
                requests.post(
                    f"{RAINSIS_URL}/api/warehouse/hold/thread-update",
                    json={"slack_message_ts": thread_ts, "thread": thread},
                    headers={"X-Bot-Token": HOLD_BOT_SECRET},
                    timeout=10,
                )
            except Exception as exc:
                logger.warning("Hold thread update (from hold handler) failed: %s", exc)
        return

    # Optionally restrict to configured channels
    if SUPPORT_CHANNEL_IDS and channel not in SUPPORT_CHANNEL_IDS:
        return

    display_user = _resolve_display_name(client, user)
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
                    "slack_user": display_user,
                    "slack_message_ts": ts,
                    "slack_text": _resolve_mentions(text, client),
                },
                headers={"X-Bot-Token": HOLD_BOT_SECRET},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            found_in = data.get("found_in", "unknown")
            action = data.get("action_taken", "none")
            batch_label = data.get("batch_label", "")
            warehouse_raw = data.get("warehouse", "")
            wh_label = "Rīga" if warehouse_raw == "riga" else "Rēzekne" if warehouse_raw == "rezekne" else ""
            wh_suffix = f" [{wh_label}]" if wh_label else ""
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
                    msg = f"Fulfillots. Pievienots HOLD sarakstam. :white_check_mark: (Picklist {batch_label}{wh_suffix})"
                elif label:
                    msg = f"Labeli izprintēti, vēl pikojas. Pievienots HOLD sarakstam. :white_check_mark: (Picklist {batch_label}{wh_suffix})"
                else:
                    msg = f"Pikojas, nav labeli izdrukāti. Pievienots HOLD sarakstam. :white_check_mark: (Picklist {batch_label}{wh_suffix})"
            elif found_in == "finished_picklist":
                msg = f"Sapikots. Jāmekle pie izsūtāmajiem :rotating_light: (Picklist {batch_label}{wh_suffix})"
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


def _resolve_mentions(text: str, client) -> str:
    for uid in re.findall(r"<@([A-Z0-9]+)>", text):
        name = _resolve_display_name(client, uid)
        text = text.replace(f"<@{uid}>", f"@{name}")
    return text


_emoji_cache: dict = {}

def _resolve_custom_emoji(text: str, client) -> str:
    """Replace :custom_emoji: tokens with __EMOJI_IMG:url__ so the frontend can render them."""
    names = re.findall(r":([a-zA-Z0-9_\-+]+):", text)
    if not names:
        return text
    global _emoji_cache
    if not _emoji_cache:
        try:
            resp = client.emoji_list()
            _emoji_cache = resp.get("emoji", {})
        except Exception as exc:
            logger.warning("Failed to fetch emoji list: %s", exc)
            return text
    for name in names:
        if name in _emoji_cache:
            url = _emoji_cache[name]
            # Follow aliases (alias:other_name)
            while url.startswith("alias:"):
                url = _emoji_cache.get(url[6:], url)
            text = text.replace(f":{name}:", f"__EMOJI_IMG:{url}__")
    return text


def _fetch_thread(client, channel: str, thread_ts: str) -> list:
    """Fetch all human replies, resolving @mentions and custom emoji."""
    try:
        resp = client.conversations_replies(channel=channel, ts=thread_ts)
        messages = resp.get("messages", [])
        result = []
        for msg in messages:
            # Skip bot messages (our own replies)
            if msg.get("bot_id") or msg.get("subtype") == "bot_message":
                continue
            if msg.get("text"):
                msg["text"] = _resolve_mentions(msg["text"], client)
                msg["text"] = _resolve_custom_emoji(msg["text"], client)
            if msg.get("user"):
                msg["user"] = _resolve_display_name(client, msg["user"])
            result.append(msg)
        return result
    except Exception as exc:
        logger.warning("Failed to fetch thread %s: %s", thread_ts, exc)
        return []


@app.event("message")
def handle_unmatched_messages(event, client):
    """Catch-all: scans #riga-warehouse messages for tracking numbers + handles thread updates."""
    channel = event.get("channel", "")
    subtype = event.get("subtype")
    text = event.get("text", "")
    ts = event.get("ts", "")
    thread_ts = event.get("thread_ts", "")

    # Thread reply in a tracked channel
    if thread_ts and thread_ts != ts:
        thread = _fetch_thread(client, channel, thread_ts)
        # Inhouse shipment thread (riga-warehouse)
        if channel == RIGA_WAREHOUSE_CHANNEL_ID:
            try:
                requests.post(
                    f"{RAINSIS_URL}/api/warehouse/inhouse-shipments/thread-update",
                    json={"slack_message_ts": thread_ts, "thread": thread},
                    headers={"X-Bot-Token": HOLD_BOT_SECRET},
                    timeout=10,
                )
            except Exception as exc:
                logger.warning("Inhouse thread update failed: %s", exc)
        # Hold order thread (support channels)
        if SUPPORT_CHANNEL_IDS and channel in SUPPORT_CHANNEL_IDS:
            try:
                requests.post(
                    f"{RAINSIS_URL}/api/warehouse/hold/thread-update",
                    json={"slack_message_ts": thread_ts, "thread": thread},
                    headers={"X-Bot-Token": HOLD_BOT_SECRET},
                    timeout=10,
                )
            except Exception as exc:
                logger.warning("Hold thread update failed: %s", exc)
        return

    if subtype is not None or channel != RIGA_WAREHOUSE_CHANNEL_ID:
        if subtype is None and _HOLD_PATTERN.search(text):
            logger.warning("HOLD pattern seen in catch-all but missed by @app.message — text: %r", text)
        return

    # Scan for tracking numbers
    trackings = _extract_trackings(text)
    if not trackings:
        return

    receiver_id = _extract_receiver(text)
    sender_id = event.get("user", "")
    receiver_name = _resolve_display_name(client, receiver_id)
    sender_name = _resolve_display_name(client, sender_id)

    for t in trackings:
        try:
            resp = requests.post(
                f"{RAINSIS_URL}/api/warehouse/inhouse-shipments/register",
                json={
                    "tracking_number": t["tracking_number"],
                    "carrier_slug": t["carrier_slug"],
                    "carrier": t["carrier"],
                    "receiver_slack_id": receiver_id,
                    "receiver_name": receiver_name,
                    "sender_slack_id": sender_id,
                    "sender_name": sender_name,
                    "slack_channel": channel,
                    "slack_message_ts": ts,
                    "slack_text": text,
                },
                headers={"X-Bot-Token": HOLD_BOT_SECRET},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("duplicate"):
                logger.info("Registered shipment %s (%s) for %s", t["tracking_number"], t["carrier"], receiver_name or sender_name)
        except Exception as exc:
            logger.warning("Failed to register shipment %s: %s", t["tracking_number"], exc)


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


def _scan_channel_history(client) -> None:
    """On startup, scan the last 100 messages in HISTORY_SCAN_CHANNEL_ID.

    Registers any unhandled HOLDs — RainSis deduplicates so already-registered
    ones are silently skipped.
    """
    if not HISTORY_SCAN_CHANNEL_ID:
        return
    for channel_id in [HISTORY_SCAN_CHANNEL_ID]:
        try:
            resp = client.conversations_history(channel=channel_id, limit=100)
            messages = resp.get("messages", [])
            logger.info("History scan: %d messages in channel %s", len(messages), channel_id)
            for msg in messages:
                text = msg.get("text", "")
                ts = msg.get("ts", "")
                user = msg.get("user", "")
                if not _HOLD_PATTERN.search(text):
                    continue
                for order_number in _HOLD_PATTERN.findall(text):
                    order_number = order_number.lstrip("#")
                    try:
                        r = requests.post(
                            f"{RAINSIS_URL}/api/warehouse/hold/register",
                            json={"order_number": order_number, "slack_channel": channel_id,
                                  "slack_user": _resolve_display_name(client, user), "slack_message_ts": ts,
                                  "slack_text": _resolve_mentions(text, client)},
                            headers={"X-Bot-Token": HOLD_BOT_SECRET},
                            timeout=10,
                        )
                        r.raise_for_status()
                        data = r.json()
                        if not data.get("duplicate"):
                            logger.info("History scan: registered HOLD #%s from channel %s", order_number, channel_id)
                    except Exception as exc:
                        logger.warning("History scan: failed to register HOLD #%s: %s", order_number, exc)
        except Exception as exc:
            logger.warning("History scan: failed to fetch history for channel %s: %s", channel_id, exc)


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    _scan_channel_history(app.client)
    handler.start()