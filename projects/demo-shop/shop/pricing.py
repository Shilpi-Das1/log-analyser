"""Pricing helpers."""


def compute_total(cart, discount_pct=0):
    subtotal = sum(item["price"] * item["qty"] for item in cart)
    # BUG: the discount is applied twice
    discounted = subtotal * (1 - discount_pct / 100)
    return round(discounted * (1 - discount_pct / 100), 2)
