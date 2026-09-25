"""Receipts: from a reading to a ledger row, and from an inbox file to both.

The unit tests pin the parsing and validation rules. The end-to-end tests run
the real parse worker as a subprocess against a stub of llama-server's chat
endpoint, so they cover the client, the extractor, the process boundary, the
validation in the parent and the ledger write -- everything except the model.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import receipts

TODAY = date(2026, 9, 25)


# --- amounts ----------------------------------------------------------------


@pytest.mark.parametrize(
    "printed, cents",
    [
        ("$1,234.56", 123456),
        ("1.234,56", 123456),
        ("12,49", 1249),
        ("1,234", 123400),
        ("12.5", 1250),
        ("-3.00", -300),
        ("3.00-", -300),
        ("(3.00)", -300),
        ("USD 12.00", 1200),
        ("12.00 EUR", 1200),
        ("1 234,56", 123456),
        ("0.005", 1),  # half a cent rounds up, not to even
    ],
)
def test_amounts_are_read_as_printed(printed, cents):
    assert receipts.parse_amount(printed) == cents


@pytest.mark.parametrize("printed", [None, "", "abc", "N/A", "--"])
def test_unreadable_amounts_are_none_not_zero(printed):
    assert receipts.parse_amount(printed) is None


# --- dates ------------------------------------------------------------------


def test_dates_in_common_forms():
    for printed in ("2026-09-24", "09/24/2026", "Sep 24, 2026", "24 September 2026", "24.09.2026"):
        assert receipts.parse_date(printed, today=TODAY) == ("2026-09-24", None), printed


def test_a_printed_time_is_ignored():
    assert receipts.parse_date("09/24/2026 14:32", today=TODAY)[0] == "2026-09-24"
    assert receipts.parse_date("2026-09-24 2:32 PM", today=TODAY)[0] == "2026-09-24"


def test_an_ambiguous_date_is_read_month_first_and_noted():
    iso, note = receipts.parse_date("03/04/26", today=TODAY)
    assert iso == "2026-03-04"
    assert "ambiguous" in note


@pytest.mark.parametrize("printed", ["2027-01-02", "1999-12-31", "not a date"])
def test_impossible_or_unreadable_dates_are_refused(printed):
    iso, note = receipts.parse_date(printed, today=TODAY)
    assert iso is None and note


# --- validation -------------------------------------------------------------


def _clean(**overrides):
    reading = {
        "merchant": "Blue Bottle Coffee",
        "date": "2026-09-24",
        "currency": "USD",
        "line_items": [
            {"description": "Latte", "quantity": "1", "amount": "5.50"},
            {"description": "Croissant", "quantity": "1", "amount": "4.25"},
        ],
        "subtotal": "9.75",
        "tax": "0.87",
        "tip": "2.00",
        "discounts": None,
        "total": "$12.62",
        "payment_method": "VISA 4242",
        "category": "dining",
    }
    reading.update(overrides)
    return reading


def _validate(reading):
    return receipts.validate(
        reading, fallback_date="2026-09-20", extracted_by="vlm:test",
        source_id="inbox:receipts", domain="receipts", today=TODAY,
    )


def test_a_clean_receipt_needs_no_review():
    row = _validate(_clean())
    assert row["amount_cents"] == 1262
    assert row["txn_date"] == "2026-09-24"
    assert row["needs_review"] == 0 and row["review_notes"] is None
    assert [i["amount_cents"] for i in json.loads(row["line_items"])] == [550, 425]


def test_line_items_that_do_not_sum_to_the_subtotal_are_flagged():
    row = _validate(_clean(subtotal="10.75"))
    assert row["needs_review"] == 1
    assert "line items sum to 9.75, subtotal says 10.75" in row["review_notes"]


def test_a_total_that_does_not_add_up_is_flagged():
    row = _validate(_clean(total="21.62"))
    assert row["needs_review"] == 1
    assert "total says 21.62" in row["review_notes"]
    assert row["amount_cents"] == 2162  # recorded as printed, flagged, not "corrected"


def test_a_one_cent_rounding_gap_is_tolerated():
    assert _validate(_clean(total="12.63"))["needs_review"] == 0


def test_discounts_reduce_the_total_whichever_sign_they_are_printed_with():
    for printed in ("1.00", "-1.00", "(1.00)"):
        assert _validate(_clean(discounts=printed, total="11.62"))["needs_review"] == 0


def test_no_total_is_recorded_as_zero_and_flagged():
    row = _validate(_clean(total=None))
    assert row["amount_cents"] == 0
    assert row["needs_review"] == 1 and "no total read" in row["review_notes"]


def test_no_date_falls_back_to_the_file_date_and_is_flagged():
    row = _validate(_clean(date=None))
    assert row["txn_date"] == "2026-09-20"
    assert row["needs_review"] == 1


def test_a_reading_of_the_wrong_shape_is_flagged_not_trusted():
    row = _validate(["not", "an", "object"])
    assert row["needs_review"] == 1
    assert "not an object" in row["review_notes"]
    row = _validate(_clean(line_items=["Latte 5.50"]))
    assert "unreadable line item" in row["review_notes"]


def test_a_full_card_number_never_reaches_the_ledger():
    row = _validate(_clean(payment_method="VISA 4111 1111 1111 1111"))
    assert "4111 1111" not in row["payment_method"]
    assert row["payment_method"].endswith("1111")


def test_an_unknown_category_is_other_and_a_missing_currency_is_usd():
    row = _validate(_clean(category="coffee", currency=None))
    assert row["category"] == "other"
    assert row["currency"] == "USD"
    assert row["needs_review"] == 0


def test_merchants_match_loosely_for_duplicates():
    assert receipts.same_merchant("COSTCO WHOLESALE #123", "Costco")
    assert receipts.same_merchant(None, "Costco")
    assert not receipts.same_merchant("Costco", "Safeway")


def test_the_searchable_text_quotes_amounts_as_printed():
    text = receipts.render_text(_clean())
    assert "Blue Bottle Coffee" in text
    assert "- 1 x Latte: 5.50" in text
    assert "Total: $12.62" in text


# --- end to end, through the real parse worker ------------------------------


class _StubVision:
    """Imitates llama-server's /v1/chat/completions with a canned reading."""

    def __init__(self, reading, status=200):
        self.reading, self.status, self.requests = reading, status, []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(body)
                payload = {"choices": [{"message": {"content": json.dumps(stub.reading)}}]}
                data = json.dumps(payload if stub.status == 200 else {"error": "boom"}).encode()
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def receipts_inbox(unlocked, tmp_path, monkeypatch):
    from app.config import settings
    from app.pipeline import state

    monkeypatch.setattr(settings, "state_db_path", tmp_path / "state.db")
    monkeypatch.setattr(settings, "data_root", tmp_path)
    state.init()
    inbox = tmp_path / "inbox" / "receipts"
    inbox.mkdir(parents=True)
    return inbox


@pytest.fixture
def vision(monkeypatch):
    stubs = []

    def start(reading, status=200):
        stub = _StubVision(reading, status)
        # The parse worker is a fresh interpreter; it reads settings from the env.
        monkeypatch.setenv("LLAMA_URL", stub.url)
        stubs.append(stub)
        return stub

    yield start
    for stub in stubs:
        stub.server.shutdown()


def _photo(path, shade=200):
    from PIL import Image

    Image.new("RGB", (3000, 4000), (shade, shade, shade)).save(path, format="JPEG")


def _ledger():
    from app.vault.keyagent import AGENT

    conn = AGENT.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT l.*, d.uri FROM ledger l JOIN documents d USING (doc_id) ORDER BY d.uri"
        )]
    finally:
        conn.close()


def _run(inbox, run_id="run-1", source_type="inbox"):
    from app.pipeline import sync
    from app.sources import Source

    source = Source(id="inbox:receipts", type=source_type, domain="receipts", path=inbox)
    return sync.sync_source(source, run_id)


def test_a_photographed_receipt_reaches_the_ledger(receipts_inbox, vision):
    stub = vision(_clean())
    _photo(receipts_inbox / "coffee.jpg")

    result = _run(receipts_inbox)
    assert result.docs_indexed == 1, result.notes
    assert result.receipts_read == 1 and result.receipts_flagged == 0

    (row,) = _ledger()
    assert (row["merchant"], row["amount_cents"], row["txn_date"]) == ("Blue Bottle Coffee", 1262, "2026-09-24")
    assert row["extracted_by"] == "vlm:qwen3-vl"
    assert not any(receipts_inbox.iterdir()), "the plaintext photo should have been consumed"

    # What was sent: one scaled-down image, and a schema to answer in.
    (request,) = stub.requests
    image = request["messages"][1]["content"][0]["image_url"]["url"]
    assert image.startswith("data:image/jpeg;base64,")
    assert request["response_format"]["type"] == "json_schema"
    assert request["temperature"] == 0


def test_the_same_receipt_filed_twice_is_flagged_not_merged(receipts_inbox, vision):
    vision(_clean())
    _photo(receipts_inbox / "coffee-a.jpg", shade=200)
    _photo(receipts_inbox / "coffee-b.jpg", shade=201)

    result = _run(receipts_inbox)
    assert result.receipts_read == 2 and result.receipts_flagged == 1

    a, b = _ledger()
    assert a["needs_review"] == 0
    assert b["needs_review"] == 1
    assert "possible duplicate of coffee-a.jpg" in b["review_notes"]


def test_a_vision_failure_leaves_the_file_for_the_next_run(receipts_inbox, vision):
    vision(_clean(), status=500)
    _photo(receipts_inbox / "coffee.jpg")

    result = _run(receipts_inbox)
    assert result.failed == 1
    assert "refused the request (500)" in " ".join(result.notes)
    assert (receipts_inbox / "coffee.jpg").exists()
    assert _ledger() == []


def test_an_unchanged_receipt_is_not_read_again(receipts_inbox, vision):
    """A receipts folder indexed in place must not cost a model call per file
    per run: the file hash is checked before the model is asked."""
    stub = vision(_clean())
    _photo(receipts_inbox / "coffee.jpg")

    _run(receipts_inbox, "run-1", source_type="local")
    second = _run(receipts_inbox, "run-2", source_type="local")
    assert second.docs_skipped == 1
    assert len(stub.requests) == 1


def test_a_retracted_receipt_leaves_the_totals(receipts_inbox, vision):
    from app.vault import service

    vision(_clean())
    _photo(receipts_inbox / "coffee.jpg")
    _run(receipts_inbox)
    assert service.query_ledger(ctx=service.Context())["total_cents"] == 1262

    service.set_lifecycle("coffee.jpg", "retracted", reason="duplicate of a card statement")
    assert service.query_ledger(ctx=service.Context())["total_cents"] == 0


def test_a_note_filed_under_receipts_is_still_text(receipts_inbox, vision):
    stub = vision(_clean())
    (receipts_inbox / "warranty.md").write_text("# Warranty\n\nThe pump is covered until 2028.\n")

    result = _run(receipts_inbox)
    assert result.docs_indexed == 1 and result.receipts_read == 0
    assert stub.requests == []
