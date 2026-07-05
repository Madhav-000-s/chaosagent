"""Canonical catalogue and customer roster.

Task fixtures are built by varying inventory levels and pre-existing orders on
top of this fixed base, so every task in the suite shares one product
catalogue. That keeps prompts short and makes cross-task comparisons legible.
"""

from __future__ import annotations

from chaosagent.types import CustomerSeed, InitState, InventorySeed, ProductSeed

CUSTOMERS: list[CustomerSeed] = [
    CustomerSeed(id="cus_1", name="Ada Byron", email="ada@example.com"),
    CustomerSeed(id="cus_2", name="Grace Hopper", email="grace@example.com"),
    CustomerSeed(id="cus_3", name="Alan Turing", email="alan@example.com"),
    CustomerSeed(id="cus_4", name="Katherine Johnson", email="katherine@example.com"),
]

PRODUCTS: list[ProductSeed] = [
    ProductSeed(sku="SKU-KEYB", name="Mechanical Keyboard", price_cents=12_900, category="peripherals"),
    ProductSeed(sku="SKU-MOUS", name="Wireless Mouse", price_cents=4_500, category="peripherals"),
    ProductSeed(sku="SKU-MONI", name="27-inch Monitor", price_cents=31_900, category="displays"),
    ProductSeed(sku="SKU-DOCK", name="Thunderbolt Dock", price_cents=22_000, category="peripherals"),
    ProductSeed(sku="SKU-CABL", name="USB-C Cable", price_cents=1_200, category="accessories"),
    ProductSeed(sku="SKU-STND", name="Laptop Stand", price_cents=5_100, category="accessories"),
    ProductSeed(sku="SKU-HDST", name="Noise-cancelling Headset", price_cents=17_500, category="audio"),
    ProductSeed(sku="SKU-WEBC", name="1080p Webcam", price_cents=8_800, category="video"),
]

SKUS: tuple[str, ...] = tuple(p.sku for p in PRODUCTS)

#: Price lookup, so task authors never hard-code a total.
PRICES: dict[str, int] = {p.sku: p.price_cents for p in PRODUCTS}

DEFAULT_ONHAND = 25


def base_inventory(overrides: dict[str, int] | None = None) -> list[InventorySeed]:
    """Full inventory at :data:`DEFAULT_ONHAND`, with per-SKU overrides."""
    overrides = overrides or {}
    return [
        InventorySeed(sku=p.sku, onhand=overrides.get(p.sku, DEFAULT_ONHAND))
        for p in PRODUCTS
    ]


def base_state(inventory_overrides: dict[str, int] | None = None) -> InitState:
    """A world with the full catalogue, all customers, and no orders."""
    return InitState(
        customers=list(CUSTOMERS),
        products=list(PRODUCTS),
        inventory=base_inventory(inventory_overrides),
        orders=[],
    )


__all__ = [
    "CUSTOMERS",
    "DEFAULT_ONHAND",
    "PRICES",
    "PRODUCTS",
    "SKUS",
    "base_inventory",
    "base_state",
]
