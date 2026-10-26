"""Keep Facebook's card price separate from an explicit seller asking price."""

from __future__ import annotations

import re


SELLER_ASK = re.compile(
    r"\b(?:asking(?:\s+price)?(?:\s+is|\s+for)?|ask|"
    r"selling(?:\s+(?:it|this))?\s+for|price\s*(?:is|:)|"
    r"(?:i\s+)?(?:want|would\s+take))\s*:?\s*\$\s*"
    r"([1-9]\d{0,5}(?:,\d{3})*(?:\.\d{1,2})?)\b",
    re.I,
)
NOT_FREE = re.compile(r"\b(?:not|isn['’]t|is\s+not)\s+free\b", re.I)


def seller_asking_price(description: str) -> int | float | None:
    """Return a single explicit dollar ask, leaving ambiguous amounts unknown."""
    prices = {float(value.replace(",", "")) for value in
              SELLER_ASK.findall(str(description or ""))}
    if len(prices) != 1:
        return None
    price = prices.pop()
    return int(price) if price.is_integer() else price


def display_price(card_price: object, description: str,
                  review_flags: list[str]) -> tuple[int | float | None, str, int | float | None]:
    """Use a seller's explicit ask when a zero-price card contradicts it."""
    if card_price != 0 or isinstance(card_price, bool):
        return card_price, "facebook_card", None
    seller_price = seller_asking_price(description)
    if seller_price is not None:
        return seller_price, "seller_description", seller_price
    if (NOT_FREE.search(str(description or "")) or
            "zero_price_card_may_not_be_free" in review_flags):
        return None, "unconfirmed", None
    return card_price, "facebook_card", None
