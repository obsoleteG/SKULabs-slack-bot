import os
import json
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from SKULabs import SKULabsClient

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"])
skulabs = SKULabsClient(os.environ["SKULABS_API_KEY"])


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