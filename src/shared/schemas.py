"""Record schema definition and validation.

Validation is implemented with the standard library only (dataclasses and
``decimal``) so the Lambda layer stays small and cold starts stay fast. The
pipeline processes order records; extending it to another dataset means
adding a dataclass with a ``from_dict`` constructor that follows the same
contract: raise :class:`ValidationError` with a list of field errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

#: Order lifecycle states accepted by the pipeline.
VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "paid", "shipped", "delivered", "cancelled", "refunded"}
)

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class ValidationError(ValueError):
    """Raised when a row (or a whole file) fails schema validation.

    ``errors`` is a list of human-readable, per-field messages so the caller
    can log every problem in one pass instead of failing on the first field.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _required_str(raw: dict[str, Any], field: str, errors: list[str]) -> str:
    """Return a stripped, non-empty string for *field*, recording errors."""
    value = raw.get(field)
    if value is None or not str(value).strip():
        errors.append(f"{field}: required and must be non-empty")
        return ""
    return str(value).strip()


@dataclass(frozen=True, slots=True)
class OrderRecord:
    """A validated, normalized order row."""

    order_id: str
    customer_id: str
    sku: str
    quantity: int
    unit_price: Decimal
    currency: str
    status: str
    order_date: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OrderRecord:
        """Validate and normalize one raw row.

        Normalization rules: strings are stripped, currency is upper-cased,
        status is lower-cased, and ``order_date`` accepts any ISO 8601 date
        or timestamp but is stored as ``YYYY-MM-DD``.

        Raises:
            ValidationError: with one message per failing field.
        """
        errors: list[str] = []

        order_id = _required_str(raw, "order_id", errors)
        customer_id = _required_str(raw, "customer_id", errors)
        sku = _required_str(raw, "sku", errors)

        quantity = 0
        try:
            quantity = int(str(raw.get("quantity", "")).strip())
            if quantity <= 0:
                errors.append("quantity: must be a positive integer")
        except ValueError:
            errors.append(f"quantity: not an integer ({raw.get('quantity')!r})")

        unit_price = Decimal("0")
        try:
            unit_price = Decimal(str(raw.get("unit_price", "")).strip())
            if not unit_price.is_finite() or unit_price < 0:
                errors.append("unit_price: must be a non-negative finite number")
        except InvalidOperation:
            errors.append(f"unit_price: not a number ({raw.get('unit_price')!r})")

        currency = str(raw.get("currency", "")).strip().upper()
        if not _CURRENCY_RE.match(currency):
            errors.append(f"currency: expected a 3-letter ISO code ({raw.get('currency')!r})")

        status = str(raw.get("status", "")).strip().lower()
        if status not in VALID_STATUSES:
            errors.append(f"status: {raw.get('status')!r} not in {sorted(VALID_STATUSES)}")

        order_date = ""
        raw_date = str(raw.get("order_date", "")).strip()
        try:
            order_date = datetime.fromisoformat(raw_date).date().isoformat()
        except ValueError:
            errors.append(f"order_date: not an ISO 8601 date ({raw.get('order_date')!r})")

        if errors:
            raise ValidationError(errors)

        return cls(
            order_id=order_id,
            customer_id=customer_id,
            sku=sku,
            quantity=quantity,
            unit_price=unit_price,
            currency=currency,
            status=status,
            order_date=order_date,
        )

    @property
    def total(self) -> Decimal:
        """Extended price for the line (quantity times unit price)."""
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))

    def to_item(self, *, source_key: str, content_hash: str, ingested_at: str) -> dict[str, Any]:
        """Build the DynamoDB item for this record, enriched with lineage."""
        return {
            "pk": f"ORDER#{self.order_id}",
            "sk": f"CUSTOMER#{self.customer_id}",
            "entity": "order",
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "sku": self.sku,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
            "total": self.total,
            "currency": self.currency,
            "status": self.status,
            "order_date": self.order_date,
            "source_key": source_key,
            "source_sha256": content_hash,
            "ingested_at": ingested_at,
        }

    def to_output(self) -> dict[str, Any]:
        """Build the JSON-safe row written to the processed bucket."""
        return {
            "order_id": self.order_id,
            "customer_id": self.customer_id,
            "sku": self.sku,
            "quantity": self.quantity,
            "unit_price": str(self.unit_price),
            "total": str(self.total),
            "currency": self.currency,
            "status": self.status,
            "order_date": self.order_date,
        }


def validate_batch(
    rows: list[dict[str, Any]],
) -> tuple[list[OrderRecord], list[dict[str, Any]]]:
    """Validate every row, splitting the batch into records and rejects.

    Returns:
        A ``(records, rejects)`` pair. Each reject is a dict with the
        zero-based ``row_index`` and the list of validation ``errors`` so it
        can be logged and counted without aborting the whole file.
    """
    records: list[OrderRecord] = []
    rejects: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        try:
            records.append(OrderRecord.from_dict(row))
        except ValidationError as exc:
            rejects.append({"row_index": index, "errors": exc.errors})
    return records, rejects
