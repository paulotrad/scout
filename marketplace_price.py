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
SELLER_OBO = re.compile(
    r"\$\s*([1-9]\d{0,5}(?:,\d{3})*(?:\.\d{1,2})?)\s*"
    r"(?:or\s+best\s+offer|obo)\b", re.I,
)
NOT_FREE = re.compile(r"\b(?:not|isn['’]t|is\s+not)\s+free\b", re.I)
CASH_PAYMENT = re.compile(
    r"\b(?:cash\s+only|cash\s+and\s+(?:pick\s*up|pickup)\s+only|"
    r"pay(?:ment)?\s+(?:in|by)\s+cash)\b", re.I)
FREE_TITLE = re.compile(
    r"^\W*free\b(?!\s+(?:delivery|shipping|quotes?|estimates?|installation))",
    re.I,
)
FREE_DESCRIPTION = re.compile(
    r"\b(?:it(?:'s| is)\s+free|for\s+free|free\s+to\s+(?:take|pick\s*up)|"
    r"giving\s+(?:it|this|them)\s+away)\b", re.I,
)
PRICE_ONLY_TITLE = re.compile(r"^\s*\$\s*[1-9]\d{0,5}(?:\.\d{1,2})?\s*$")


def clear_free_offer(title: str, description: str) -> bool:
    """Recognize an affirmative free-item claim, excluding free delivery ads."""
    return bool(FREE_TITLE.search(str(title or "")) or
                FREE_DESCRIPTION.search(str(description or "")))


def seller_asking_price(description: str) -> int | float | None:
    """Return a single explicit dollar ask, leaving ambiguous amounts unknown."""
    prices = {float(value.replace(",", "")) for value in
              [*SELLER_ASK.findall(str(description or "")),
               *SELLER_OBO.findall(str(description or ""))]}
    if len(prices) != 1:
        return None
    price = prices.pop()
    return int(price) if price.is_integer() else price


def display_price(card_price: object, description: str,
                  review_flags: list[str], title: str = "") -> tuple[int | float | None, str, int | float | None]:
    """Use a seller's explicit ask when a zero-price card contradicts it."""
    if card_price != 0 or isinstance(card_price, bool):
        return card_price, "facebook_card", None
    seller_price = seller_asking_price(description)
    title_price = seller_asking_price(title)
    if seller_price is not None and title_price is not None and seller_price != title_price:
        return None, "unconfirmed", None
    if seller_price is not None:
        return seller_price, "seller_description", seller_price
    if title_price is not None:
        return title_price, "seller_title", title_price
    if (NOT_FREE.search(f"{title} {description}") or
            CASH_PAYMENT.search(str(description or "")) or
            PRICE_ONLY_TITLE.fullmatch(str(title or "")) or
            ("zero_price_card_may_not_be_free" in review_flags and
             not clear_free_offer(title, description))):
        return None, "unconfirmed", None
    return card_price, "facebook_card", None
