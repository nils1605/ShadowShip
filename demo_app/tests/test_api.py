from fastapi.testclient import TestClient

from demo_app.main import app

client = TestClient(app)


def _cart_with(*items):
    cart_id = client.post("/cart").json()["id"]
    for product_id, quantity in items:
        client.post(f"/cart/{cart_id}/items", json={"product_id": product_id, "quantity": quantity})
    return cart_id


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_products_listed():
    products = client.get("/products").json()
    assert {p["id"] for p in products} >= {"mug", "tshirt", "hoodie"}


def test_add_unknown_product_returns_404():
    cart_id = client.post("/cart").json()["id"]
    response = client.post(f"/cart/{cart_id}/items", json={"product_id": "nope"})
    assert response.status_code == 404


def test_checkout_without_discount():
    cart_id = _cart_with(("mug", 2))
    body = client.post("/checkout", json={"cart_id": cart_id}).json()
    assert body["subtotal"] == 24.00
    assert body["discount"] == 0.0
    assert body["tax"] == 1.92
    assert body["total"] == 25.92


def test_checkout_rejects_unknown_code():
    cart_id = _cart_with(("mug", 1))
    response = client.post("/checkout", json={"cart_id": cart_id, "discount_code": "BOGUS"})
    assert response.status_code == 400


def test_checkout_empty_cart_rejected():
    cart_id = client.post("/cart").json()["id"]
    assert client.post("/checkout", json={"cart_id": cart_id}).status_code == 400
