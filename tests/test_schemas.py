"""Unit tests for shared.schemas."""

from decimal import Decimal

import pytest

from shared.schemas import OrderRecord, ValidationError, validate_batch

VALID_ROW = {
    "order_id": "ord-1001",
    "customer_id": "cust-42",
    "sku": "SKU-RED-M",
    "quantity": "3",
    "unit_price": "19.99",
    "currency": "usd",
    "status": "PAID",
    "order_date": "2026-06-30T14:05:00Z",
}


class TestOrderRecordFromDict:
    def test_valid_row_is_normalized(self):
        record = OrderRecord.from_dict(VALID_ROW)

        assert record.order_id == "ord-1001"
        assert record.quantity == 3
        assert record.unit_price == Decimal("19.99")
        assert record.currency == "USD"
        assert record.status == "paid"
        assert record.order_date == "2026-06-30"

    def test_total_is_quantity_times_unit_price(self):
        record = OrderRecord.from_dict(VALID_ROW)
        assert record.total == Decimal("59.97")

    def test_whitespace_is_stripped(self):
        record = OrderRecord.from_dict({**VALID_ROW, "order_id": "  ord-2  ", "sku": " X "})
        assert record.order_id == "ord-2"
        assert record.sku == "X"

    def test_plain_date_is_accepted(self):
        record = OrderRecord.from_dict({**VALID_ROW, "order_date": "2026-01-15"})
        assert record.order_date == "2026-01-15"

    @pytest.mark.parametrize(
        ("field", "value", "fragment"),
        [
            ("order_id", "", "order_id"),
            ("customer_id", None, "customer_id"),
            ("quantity", "zero", "quantity"),
            ("quantity", "0", "quantity"),
            ("quantity", "-2", "quantity"),
            ("unit_price", "free", "unit_price"),
            ("unit_price", "-1.00", "unit_price"),
            ("currency", "dollars", "currency"),
            ("status", "teleported", "status"),
            ("order_date", "30/06/2026", "order_date"),
        ],
    )
    def test_invalid_field_is_reported(self, field, value, fragment):
        with pytest.raises(ValidationError) as excinfo:
            OrderRecord.from_dict({**VALID_ROW, field: value})
        assert any(fragment in error for error in excinfo.value.errors)

    def test_multiple_errors_are_collected(self):
        with pytest.raises(ValidationError) as excinfo:
            OrderRecord.from_dict({})
        assert len(excinfo.value.errors) >= 5

    def test_to_item_includes_lineage(self):
        record = OrderRecord.from_dict(VALID_ROW)
        item = record.to_item(
            source_key="s3://raw/orders.csv",
            content_hash="abc123",
            ingested_at="2026-07-01T00:00:00+00:00",
        )

        assert item["pk"] == "ORDER#ord-1001"
        assert item["sk"] == "CUSTOMER#cust-42"
        assert item["source_sha256"] == "abc123"
        assert item["total"] == Decimal("59.97")

    def test_to_output_is_json_safe(self):
        import json

        output = OrderRecord.from_dict(VALID_ROW).to_output()
        assert json.loads(json.dumps(output)) == output
        assert output["unit_price"] == "19.99"


class TestValidateBatch:
    def test_splits_valid_and_invalid_rows(self):
        rows = [VALID_ROW, {**VALID_ROW, "quantity": "-1"}, {**VALID_ROW, "order_id": "ord-2"}]
        records, rejects = validate_batch(rows)

        assert [record.order_id for record in records] == ["ord-1001", "ord-2"]
        assert len(rejects) == 1
        assert rejects[0]["row_index"] == 1
        assert any("quantity" in error for error in rejects[0]["errors"])

    def test_empty_batch(self):
        assert validate_batch([]) == ([], [])
