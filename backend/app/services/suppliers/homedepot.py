"""Home Depot product search via their public-facing GraphQL API."""

import asyncio
import logging

import httpx

from backend.app.services.suppliers.protocol import Location, ProductResult

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://apionline.homedepot.com/federation-gateway/graphql"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.homedepot.com",
    "Referer": "https://www.homedepot.com/",
    "x-experience-name": "general-merchandise",
    "x-hd-dc": "origin",
    "x-debug": "false",
}

_SEARCH_QUERY = """
query searchModel(
  $keyword: String!
  $storeId: String
  $pageSize: Int
  $startIndex: Int
  $channel: Channel
  $additionalSearchParams: AdditionalParams
) {
  searchModel(
    keyword: $keyword
    storeId: $storeId
    pageSize: $pageSize
    startIndex: $startIndex
    channel: $channel
    additionalSearchParams: $additionalSearchParams
  ) {
    products {
      itemId
      identifiers {
        brandName
        modelNumber
        productLabel
        canonicalUrl
      }
      pricing {
        value
        original
      }
      ratingsReviews {
        averageRating
        totalReviews
      }
      media {
        images {
          url
        }
      }
      fulfillment {
        backordered
        fulfillmentOptions {
          type
          services {
            type
            locations {
              inventory {
                isInStock
                isLimitedQuantity
                quantity
              }
              aisle {
                bay
                aisle
              }
            }
          }
        }
      }
    }
  }
}
""".strip()


class HomeDepotSupplier:
    """Home Depot product search via their public GraphQL API."""

    def __init__(self, store_id: str = "") -> None:
        self.store_id = store_id
        self.name = "homedepot"
        self.display_name = "Home Depot"

    async def _request(self, payload: dict) -> dict:
        """POST to the GraphQL endpoint with retry on 5xx."""
        async with httpx.AsyncClient(timeout=20.0) as client:
            for attempt in range(2):
                resp = await client.post(
                    _GRAPHQL_URL,
                    headers=_HEADERS,
                    json=payload,
                    params={"opname": payload.get("operationName", "")},
                )
                if resp.status_code >= 500 and attempt == 0:
                    logger.warning("Home Depot API server error %d, retrying", resp.status_code)
                    await asyncio.sleep(1.0)
                    continue
                resp.raise_for_status()
                return resp.json()
        return {}

    async def search_products(
        self, query: str, location: Location, *, max_results: int = 5
    ) -> list[ProductResult]:
        payload = {
            "operationName": "searchModel",
            "variables": {
                "keyword": query,
                "storeId": self.store_id or "",
                "pageSize": max_results,
                "startIndex": 0,
                "channel": "DESKTOP",
                "additionalSearchParams": {
                    "deliveryZip": location.zip_code,
                },
            },
            "query": _SEARCH_QUERY,
        }
        data = await self._request(payload)

        # Check for GraphQL-level errors (HD returns these as {"error": [...]} or {"errors": [...]})
        gql_errors = data.get("error") or data.get("errors")
        if gql_errors:
            logger.warning(
                "Home Depot GraphQL error for query=%r zip=%s: %s",
                query,
                location.zip_code,
                gql_errors,
            )

        products_raw = data.get("data", {}).get("searchModel", {}).get("products") or []
        if not products_raw:
            logger.info(
                "Home Depot returned 0 products for query=%r zip=%s (response keys: %s)",
                query,
                location.zip_code,
                list(data.get("data", {}).keys()) if data.get("data") else "no data",
            )

        results: list[ProductResult] = []
        for product in products_raw[:max_results]:
            ids = product.get("identifiers") or {}
            pricing = product.get("pricing") or {}
            reviews = product.get("ratingsReviews") or {}
            images = (product.get("media") or {}).get("images") or []
            fulfillment = product.get("fulfillment") or {}

            # Extract inventory from the first pickup service location
            in_stock = None
            stock_qty = None
            aisle_str = ""
            for opt in fulfillment.get("fulfillmentOptions") or []:
                for svc in opt.get("services") or []:
                    for loc in svc.get("locations") or []:
                        inv = loc.get("inventory") or {}
                        if in_stock is None:
                            in_stock = inv.get("isInStock")
                            stock_qty = inv.get("quantity")
                        aisle_info = loc.get("aisle") or {}
                        if aisle_info.get("aisle") and not aisle_str:
                            bay = aisle_info.get("bay", "")
                            aisle_str = aisle_info["aisle"]
                            if bay:
                                aisle_str += f", Bay {bay}"

            canonical = ids.get("canonicalUrl", "")
            product_url = f"https://www.homedepot.com{canonical}" if canonical else ""

            results.append(
                ProductResult(
                    supplier="homedepot",
                    product_id=str(product.get("itemId", "")),
                    name=ids.get("productLabel", "Unknown product"),
                    brand=ids.get("brandName", ""),
                    price_dollars=pricing.get("value"),
                    was_price_dollars=pricing.get("original"),
                    in_stock=in_stock,
                    stock_quantity=stock_qty,
                    aisle=aisle_str,
                    product_url=product_url,
                    image_url=images[0].get("url", "") if images else "",
                    rating=reviews.get("averageRating"),
                )
            )
        return results
