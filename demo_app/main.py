"""Demo shop service that ShadowShip rehearses releases of.

Run it with: python -m uvicorn demo_app.main:app --port 8001
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from demo_app import pricing

app = FastAPI(title="ShadowShip Demo Shop")

PRODUCTS = {
    "mug": {"id": "mug", "name": "Coffee Mug", "price": 12.00},
    "tshirt": {"id": "tshirt", "name": "T-Shirt", "price": 20.00},
    "notebook": {"id": "notebook", "name": "Notebook", "price": 5.50},
    "hoodie": {"id": "hoodie", "name": "Hoodie", "price": 45.00},
    "stickers": {"id": "stickers", "name": "Sticker Pack", "price": 3.00},
}

CARTS: dict[str, list[dict]] = {}


class AddItem(BaseModel):
    product_id: str
    quantity: int = 1


class Checkout(BaseModel):
    cart_id: str
    discount_code: str | None = None


def _get_cart(cart_id: str) -> list[dict]:
    if cart_id not in CARTS:
        raise HTTPException(status_code=404, detail="Cart not found")
    return CARTS[cart_id]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/products")
def list_products() -> list[dict]:
    return list(PRODUCTS.values())


@app.post("/cart")
def create_cart() -> dict:
    cart_id = uuid.uuid4().hex
    CARTS[cart_id] = []
    return {"id": cart_id, "items": []}


@app.post("/cart/{cart_id}/items")
def add_item(cart_id: str, item: AddItem) -> dict:
    cart = _get_cart(cart_id)
    if item.product_id not in PRODUCTS:
        raise HTTPException(status_code=404, detail="Product not found")
    if item.quantity < 1:
        raise HTTPException(status_code=400, detail="Quantity must be at least 1")
    product = PRODUCTS[item.product_id]
    cart.append({"product_id": product["id"], "price": product["price"], "quantity": item.quantity})
    return {"id": cart_id, "items": cart, "subtotal": pricing.subtotal(cart)}


@app.post("/checkout")
def checkout(request: Checkout) -> dict:
    cart = _get_cart(request.cart_id)
    if not cart:
        raise HTTPException(status_code=400, detail="Cart is empty")

    subtotal = pricing.subtotal(cart)
    try:
        discount = pricing.discount_for(request.discount_code, subtotal)
    except pricing.InvalidDiscountCode:
        raise HTTPException(status_code=400, detail="Invalid discount code")
    discounted = pricing.money(subtotal - discount)
    tax = pricing.tax_on(discounted)

    return {
        "cart_id": request.cart_id,
        "item_count": sum(item["quantity"] for item in cart),
        "subtotal": subtotal,
        "discount": discount,
        "tax": tax,
        "total": pricing.money(discounted + tax),
    }
