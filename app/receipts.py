"""Receipt readings: what the vision model is asked for, and how its answer
becomes a ledger row.

Dependency-free, like app/documents.py, because both halves of vault
ingestion need it: the keyless parse worker asks the model with `SCHEMA` and
renders the reading as searchable text, and the key-holding parent validates
the reading before anything reaches the ledger. The parent is the authority.
It re-runs `validate` itself rather than trusting a verdict computed in the
process that parsed attacker-influenced bytes.

### Why the model returns strings

Amounts come back exactly as printed (`"$1,234.56"`, `"(3.00)"`), and are
turned into integer cents here with `Decimal`. A model asked for a number
will round, drop a sign, or quietly do arithmetic; a model asked to copy text
only has to read. The same goes for dates: an ambiguous `03/04/25` is read
here, where the ambiguity can be noted, not normalised silently upstream.

### Why a doubtful reading is still a row

A receipt whose total cannot be read still gets a ledger row, at 0 cents and
flagged. Leaving it out would hide it; `needs_review_count` on every
`query_ledger` response is how a total the caller trusts says it may be short.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Domains whose images and PDFs are read as receipts. A markdown note filed
# under `receipts` is still just text.
RECEIPT_DOMAINS = frozenset({"receipts"})
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
RECEIPT_SUFFIXES = IMAGE_SUFFIXES | {".pdf"}

CATEGORIES = (
    "groceries", "dining", "fuel", "household", "electronics",
    "health", "travel", "utilities", "other",
)
DEFAULT_CURRENCY = "USD"
EARLIEST_DATE = date(2000, 1, 1)
# Rounding on a printed receipt can leave a cent between the lines and the total.
TOLERANCE_CENTS = 1

_AMOUNT_FIELD = {"type": ["string", "null"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "merchant": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"], "description": "exactly as printed"},
        "currency": {"type": ["string", "null"], "description": "ISO 4217 code if shown"},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["string", "null"]},
                    "amount": _AMOUNT_FIELD,
                },
                "required": ["description", "amount"],
            },
        },
        "subtotal": _AMOUNT_FIELD,
        "tax": _AMOUNT_FIELD,
        "tip": _AMOUNT_FIELD,
        "discounts": _AMOUNT_FIELD,
        "total": _AMOUNT_FIELD,
        "payment_method": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"], "enum": [*CATEGORIES, None]},
    },
    "required": ["merchant", "date", "line_items", "total"],
}

# What the model is told depends on what it is looking at. Each rule below
# comes from a misread of a real receipt, and one of them cost the other:
#
# - A PDF's text layer puts a wrapped description on its own line. The model
#   read each continuation as another item, and listed the subtotal and tax as
#   items too. TEXT_RULES fixed that PDF exactly.
# - Given the same rules, a PHOTO of the same receipt came back with every
#   description and no amounts at all: told a line without a price belongs to
#   the item above, it stopped pairing prices with lines it could see. So
#   images get only the rule their own misread needed -- a "Reg Price 10.00"
#   taken as the price of a line charged at 0.00.
_BASE_PROMPT = (
    "You transcribe receipts into JSON. Copy every amount and the date EXACTLY as "
    "printed, including currency symbols and signs -- do not calculate, round, or "
    "reformat. Use null for anything not printed.\n"
    "An item's amount is what was charged for that line, not a unit price, a "
    "'Reg Price', or a savings figure; a credit or refund line keeps its minus sign.\n"
    "For payment_method give only the card brand and last four digits, never a full "
    "card number. Choose the category from the allowed list."
)
TEXT_RULES = (
    "\nThis is the receipt's extracted text, not an image. line_items are the "
    "products and services purchased, nothing else: subtotal, tax, tip, discounts, "
    "total, payment, change and balance lines are NEVER line items; put them in their "
    "own fields. A description that wraps onto following lines is one item: a line "
    "with no price of its own belongs to the item above it."
)


def prompt_for(input_kind: str) -> str:
    """The instructions for a receipt read from `text` or from an `image`."""
    return _BASE_PROMPT + (TEXT_RULES if input_kind == "text" else "")


_DATE_FORMATS = (
    ("%Y-%m-%d", False), ("%Y/%m/%d", False),
    ("%m/%d/%Y", True), ("%m/%d/%y", True), ("%m-%d-%Y", True), ("%m-%d-%y", True),
    ("%d.%m.%Y", False), ("%d.%m.%y", False),
    ("%d %b %Y", False), ("%d %B %Y", False),
    ("%b %d, %Y", False), ("%B %d, %Y", False), ("%b %d %Y", False), ("%B %d %Y", False),
)
_CARD_RE = re.compile(r"\d[\d -]{8,}\d")


def parse_amount(value) -> int | None:
    """A printed amount as integer cents, or None if it cannot be read.

    `$1,234.56`, `1.234,56`, `12,49`, `-3.00`, `3.00-` and `(3.00)` are all
    understood; anything else is None rather than a guess.
    """
    if value is None:
        return None
    text = str(value).strip()
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    text = re.sub(r"[^\d.,\-]", "", text)
    if text.endswith("-"):
        negative, text = True, text[:-1]
    if text.startswith("-"):
        negative, text = True, text[1:]
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else text.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?", text):
        return None
    try:
        cents = int((Decimal(text) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None
    return -cents if negative else cents


def parse_date(value, *, today: date, month_first: bool = False) -> tuple[str | None, str | None]:
    """A printed date as ISO, plus a note when the reading needs a human.

    `month_first` says the receipt comes from a month/day country -- a USD
    receipt. There `4/12/2026` is April 12th by convention, not a coin flip,
    and noting it would flag four US receipts in ten for nothing.

    Returns (None, note) for a date that cannot be read, or that falls before
    2000 or after today -- a misread year is the commonest failure, and a
    receipt from the future is always one.
    """
    if not value:
        return None, "no date read"
    text = re.sub(r"\s+", " ", str(value)).strip()
    # Receipts print the time beside the date; it is not part of it.
    text = re.sub(r"[ T]\d{1,2}:\d{2}(:\d{2})?\s*([AaPp][Mm])?$", "", text).strip()
    for fmt, slash_form in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if not EARLIEST_DATE <= parsed <= today:
            return None, f"date {text!r} read as {parsed.isoformat()}, which cannot be right"
        note = None
        if slash_form and not month_first and parsed.day <= 12 and parsed.day != parsed.month:
            note = f"date {text!r} is ambiguous; read as month/day ({parsed.isoformat()})"
        return parsed.isoformat(), note
    return None, f"date {text!r} not understood"


def merchant_key(name: str | None) -> str:
    """The first word of a merchant name, for loose duplicate matching:
    "COSTCO WHOLESALE #123" and "Costco" are the same shop."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    words = [w for w in words if not w.isdigit()]
    return words[0] if words else ""


def same_merchant(a: str | None, b: str | None) -> bool:
    """Loose, because the date and amount have already matched exactly. An
    unreadable merchant on either side counts as a match."""
    ka, kb = merchant_key(a), merchant_key(b)
    return not ka or not kb or ka == kb


def _payment(value) -> str | None:
    if not value:
        return None
    # Belt and braces: the prompt asks for brand and last four only.
    text = _CARD_RE.sub(lambda m: "…" + re.sub(r"\D", "", m.group())[-4:], str(value))
    return text[:40]


def _money(cents: int | None) -> str:
    return "?" if cents is None else f"{cents / 100:.2f}"


def validate(
    raw: dict,
    *,
    fallback_date: str,
    extracted_by: str,
    source_id: str,
    domain: str,
    today: date | None = None,
) -> dict:
    """A ledger row from one reading, with every doubt recorded.

    Any problem sets `needs_review` and is listed in `review_notes`. The row
    is always returned: a receipt that cannot be trusted is flagged, not lost.
    """
    today = today or datetime.now(timezone.utc).date()
    notes: list[str] = []
    if not isinstance(raw, dict):
        # It crossed from the process that parsed the file; trust its shape no
        # more than its content.
        notes.append(f"reading was not an object ({type(raw).__name__})")
        raw = {}

    total = parse_amount(raw.get("total"))
    if total is None:
        notes.append(f"no total read ({raw.get('total')!r}); recorded as 0")

    currency = (raw.get("currency") or "").strip().upper()
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        notes.append(f"currency {currency!r} not an ISO code; recorded as {DEFAULT_CURRENCY}")
        currency = ""
    currency = currency or DEFAULT_CURRENCY

    txn_date, date_note = parse_date(raw.get("date"), today=today, month_first=currency == "USD")
    if date_note:
        notes.append(date_note)
    if txn_date is None:
        txn_date = fallback_date
        notes.append(f"used the file's date, {fallback_date}")

    items = []
    lines = raw.get("line_items")
    for item in lines if isinstance(lines, list) else []:
        if not isinstance(item, dict):
            notes.append(f"unreadable line item {item!r}")
            continue
        cents = parse_amount(item.get("amount"))
        if cents is None and item.get("amount") is not None:
            notes.append(f"line {item.get('description')!r}: amount {item.get('amount')!r} not read")
        items.append({
            "description": str(item.get("description") or "")[:120],
            "quantity": item.get("quantity"),
            "amount_cents": cents,
        })
    items_sum = sum(i["amount_cents"] or 0 for i in items)

    subtotal = parse_amount(raw.get("subtotal"))
    tax = parse_amount(raw.get("tax")) or 0
    tip = parse_amount(raw.get("tip")) or 0
    discounts = abs(parse_amount(raw.get("discounts")) or 0)

    if items and subtotal is not None and abs(items_sum - subtotal) > TOLERANCE_CENTS:
        notes.append(f"line items sum to {_money(items_sum)}, subtotal says {_money(subtotal)}")
    base = subtotal if subtotal is not None else (items_sum if items else None)
    if total is not None and base is not None:
        expected = base + tax + tip - discounts
        if abs(expected - total) > TOLERANCE_CENTS:
            notes.append(
                f"{'subtotal' if subtotal is not None else 'line items'} + tax + tip - discounts "
                f"= {_money(expected)}, total says {_money(total)}"
            )
    if total is not None and total < 0:
        notes.append(f"negative total {_money(total)} (a refund?)")

    category = (raw.get("category") or "").strip().lower()
    category = category if category in CATEGORIES else "other"

    return {
        "source_id": source_id,
        "domain": domain,
        "txn_date": txn_date,
        "merchant": (raw.get("merchant") or "").strip()[:120] or None,
        "amount_cents": total if total is not None else 0,
        "currency": currency,
        "category": category,
        "payment_method": _payment(raw.get("payment_method")),
        "description": f"{len(items)} line item(s)" if items else None,
        "line_items": json.dumps(items),
        "confidence": None,
        "extracted_by": extracted_by,
        "needs_review": 1 if notes else 0,
        "review_notes": "; ".join(notes) or None,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def render_text(raw: dict) -> str:
    """The reading as plain text: what `search_vault` finds and quotes.

    Amounts appear as printed, so a quoted figure matches the paper.
    """
    lines = [raw.get("merchant") or "Unknown merchant"]
    if raw.get("date"):
        lines.append(f"Date: {raw['date']}")
    for item in raw.get("line_items") or []:
        qty = f"{item['quantity']} x " if item.get("quantity") else ""
        lines.append(f"- {qty}{item.get('description', '')}: {item.get('amount') or '?'}")
    for label in ("subtotal", "tax", "tip", "discounts", "total"):
        if raw.get(label):
            lines.append(f"{label.capitalize()}: {raw[label]}")
    if raw.get("payment_method"):
        lines.append(f"Paid with: {_payment(raw['payment_method'])}")
    if raw.get("category"):
        lines.append(f"Category: {raw['category']}")
    return "\n".join(lines)
