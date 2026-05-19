import json
import time
import requests


class SKULabsClient:
    BASE_URL = "https://api.skulabs.com"
    _archived_cache: set = None
    _archived_cache_ts: float = 0
    ARCHIVED_CACHE_TTL = 300  # seconds (5 minutes)

    def __init__(self, api_key):
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

    def get_item_by_sku(self, sku: str):
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
            "item_id": item_id,
            "locations": locations
        }

    def get_archived_order_numbers(self) -> set:
        now = time.time()
        if self._archived_cache is not None and now - self._archived_cache_ts < self.ARCHIVED_CACHE_TTL:
            return self._archived_cache

        from datetime import date, timedelta
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=730)).isoformat()
        res = requests.get(
            f"{self.BASE_URL}/order/get_archived",
            headers=self.headers,
            params={"start": start, "end": end}
        )
        res.raise_for_status()
        orders = res.json().get("orders", [])
        result = {str(o.get("order_number")) for o in orders if o.get("order_number")}

        SKULabsClient._archived_cache = result
        SKULabsClient._archived_cache_ts = now
        return result

    def get_open_orders_for_item(self, sku: str) -> list:
        closed_statuses = {"shipped", "cancelled", "cleared", "partially cleared", "archived"}
        open_statuses = [
            "unstarted", "in_progress", "awaiting fulfillment",
            "pending", "delayed", "stopped", "partially shipped"
        ]

        # Fetch all open orders, paginated
        selector = {"status": {"$in": open_statuses}}
        PAGE = 250
        all_orders = []
        offset = 0
        while True:
            res = requests.get(
                f"{self.BASE_URL}/order/get",
                headers=self.headers,
                params={
                    "selector": json.dumps(selector),
                    "sort": json.dumps({"_id": -1}),
                    "limit": PAGE,
                    "skip": offset
                }
            )
            res.raise_for_status()
            page = res.json()
            all_orders.extend(page)
            if len(page) < PAGE:
                break
            offset += PAGE

        archived_numbers = self.get_archived_order_numbers()

        def fulfillable_qty(line, sku):
            if (line.get("lineSku") or line.get("sku") or "").upper() != sku.upper():
                return 0
            foi_items = (line.get("data") or {}).get("associatedFulfillmentOrderLineItems", [])
            if foi_items:
                return sum(
                    foi.get("fulfillableQuantity", 0)
                    for foi in foi_items
                    if foi.get("fulfillmentOrderStatus", "").lower() in ("open", "in_progress")
                )
            # Fallback for draft orders and non-Shopify orders
            return max(0, line.get("quantity", 0) - (line.get("shipped_quantity") or 0))

        results = []
        for order in all_orders:
            if order.get("status") in closed_statuses:
                continue
            if str(order.get("order_number") or "") in archived_numbers:
                continue
            # Skip orders with a pending cancellation/return not yet received in SKULabs
            has_pending_return = any(
                item.get("returning", 0) > item.get("returned", 0)
                for r in order.get("returns", [])
                for item in r.get("items", [])
            )
            if has_pending_return:
                continue
            qty = sum(
                fulfillable_qty(line, sku)
                for line in order.get("stash", {}).get("items", [])
            )
            if qty > 0:
                results.append({
                    "order_number": order.get("order_number"),
                    "qty": qty,
                })
        return results

    def get_purchase_orders_for_item(self, item_id: str) -> list:
        # Step 1: find which POs contain this item (returns headers only, no line items)
        res = requests.get(
            f"{self.BASE_URL}/purchase_order/get_processing_with_item_id",
            headers=self.headers,
            params={"item_id": item_id}
        )
        res.raise_for_status()
        po_headers = res.json().get("purchase_orders", [])

        results = []
        for header in po_headers:
            po_id = header.get("_id")
            # Step 2: fetch full PO to get line items
            res2 = requests.get(
                f"{self.BASE_URL}/purchase_order/get",
                headers=self.headers,
                params={"selector": json.dumps({"_id": po_id})}
            )
            res2.raise_for_status()
            full_pos = res2.json()
            if not full_pos:
                continue
            po = full_pos[0]
            incoming_qty = 0
            for line in po.get("items", []):
                if line.get("item_id") == item_id:
                    incoming_qty += line.get("quantity", 0) - line.get("received", 0)
            results.append({
                "po_number": po.get("number") or po.get("_id"),
                "expected_date": po.get("arrival_date") or po.get("due_date"),
                "incoming_qty": incoming_qty
            })
        return results
