import json
import requests


class SKULabsClient:
    BASE_URL = "https://api.skulabs.com"

    def __init__(self, api_key):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def get_item_by_sku(self, sku: str):
        # Step 1: get item by SKU
        res = requests.get(
            f"{self.BASE_URL}/item/get",
            headers=self.headers,
            params={"selector": json.dumps({"sku": sku})}
        )
        res.raise_for_status()
        items = res.json()

        if not items:
            return None

        item = items[0]
        item_id = item["_id"]

        # Step 2: get stock levels for that item
        res2 = requests.get(
            f"{self.BASE_URL}/item/get_locations",
            headers=self.headers,
            params={"item_id": item_id}
        )
        res2.raise_for_status()
        locations = res2.json()

        return {
            "name": item["name"],
            "sku": item["sku"],
            "locations": locations
        }