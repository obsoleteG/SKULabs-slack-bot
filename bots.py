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

        locations = data["locations"]
        total = sum(loc["items"][0]["on_hand"] for loc in locations if loc["items"])

        lines = [f"*{data['name']}*", f"SKU: `{data['sku']}`", ""]
        for loc in locations:
            qty = loc["items"][0]["on_hand"] if loc["items"] else 0
            if qty > 0:
                lines.append(f"• {loc['name']}: *{qty}*")

        lines.append(f"\n*Total in stock: {total}*")
        respond("\n".join(lines))

    except Exception as e:
        respond(f"Error: {e}")


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()