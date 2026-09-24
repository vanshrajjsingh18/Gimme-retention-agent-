"""The phone column in a segment export.

An exported list exists to be used somewhere else — uploaded to a provider,
handed to an agency, dialled. A New Zealand mobile written as "02902076762"
is fine on a screen and wrong in almost any of those places, so the export
writes it the way a system that has to send to it needs: +642902076762.

The whole feature is one column. What these tests are really protecting is
the two ways a formatter like this goes wrong: converting a number that was
already converted, and inventing digits for one it could not read.
"""
from __future__ import annotations

import csv
import io

import pytest

from app.core.enums import SegmentType
from app.core.phone import export_phone
from app.models.base import utcnow
from app.models.entities import Customer, CustomerSegment, Segment


# ==========================================================================
# The formatter (tests 1-7 from the brief)
# ==========================================================================
@pytest.mark.parametrize(
    "stored,expected",
    [
        # The brief's own examples.
        ("02902076762", "+642902076762"),
        ("02 902 076 762", "+642902076762"),
        ("02-902-076-762", "+642902076762"),
        ("+64 29 020 76762", "+642902076762"),
        ("642902076762", "+642902076762"),
        ("+642902076762", "+642902076762"),
        ("0211234567", "+64211234567"),
        # Shapes real exports arrive in.
        ("(021) 123-4567", "+64211234567"),
        ("0064211234567", "+64211234567"),
        ("  021 123 4567  ", "+64211234567"),
    ],
)
def test_a_stored_number_is_written_in_international_form(stored, expected):
    assert export_phone(stored) == expected


def test_an_already_international_number_is_not_converted_twice():
    """The failure this formatter exists to avoid.

    Running a conversion over its own output is the classic way a phone
    column ends up as +6464…, and it happens the first time somebody exports
    a file that was itself imported from an export.
    """
    once = export_phone("02902076762")
    assert once == "+642902076762"
    assert export_phone(once) == once, "converting twice changed the number"
    # And a bare country code is completed rather than prefixed again.
    assert export_phone("642902076762") == "+642902076762"


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_a_customer_with_no_number_gets_an_empty_cell(missing):
    """Blank, not "None" — an export is read by people and by spreadsheets."""
    assert export_phone(missing) == ""
    assert export_phone(missing) is not None


@pytest.mark.parametrize(
    "rubbish",
    ["not a phone", "0", "123", "00000", "phone: ask Sam", "021", "+", "999999999999999999"],
)
def test_an_unreadable_number_is_left_blank_rather_than_invented(rubbish):
    """No digits are manufactured from something that is not a number.

    A wrong number in an export is a text, or a call, to a stranger. Blank is
    recoverable; plausible-but-wrong is not, because nobody goes looking for
    it.
    """
    assert export_phone(rubbish) == ""


def test_a_landline_is_left_blank_rather_than_half_converted():
    """Deliberate, and worth stating: this column is mobiles.

    The canonical normaliser refuses landlines because they cannot receive an
    SMS, and the export reuses it rather than keeping a second, more generous
    idea of what a phone number is. The cost is that a customer whose only
    number is a landline exports blank — visible, and chaseable.
    """
    assert export_phone("09 555 1234") == ""
    assert export_phone("03 555 1234") == ""


def test_another_countrys_number_is_passed_through_not_made_into_a_kiwi_one():
    """An Australian +61 is not ours to reinterpret as +64."""
    assert export_phone("+61412345678") == "+61412345678"


# ==========================================================================
# The export (tests 8-10 from the brief)
# ==========================================================================
@pytest.fixture()
def segment_with_customers(db, bootstrapped):
    """Sam from the acceptance test, plus the two edge cases beside him."""
    stamp = int(utcnow().timestamp() * 1000)
    people = [
        ("Sam", "Test", "sam@test.com", "02902076762"),
        ("John", "Jones", "john@example.com", "0211234567"),
        ("Nina", "NoPhone", "nina@example.com", None),
        ("Lou", "Landline", "lou@example.com", "09 555 1234"),
    ]
    segment = Segment(
        name=f"Export test {stamp}",
        segment_type=SegmentType.MANUAL.value,
        rule_definition={},
    )
    db.add(segment)
    db.flush()

    customers = []
    for index, (first, last, email, phone) in enumerate(people):
        customer = Customer(
            external_id=f"EXPORT-{stamp}-{index}",
            first_name=first,
            last_name=last,
            email=email,
            phone=phone,
            age_verified=True,
            marketing_consent=True,
        )
        db.add(customer)
        db.flush()
        db.add(CustomerSegment(segment_id=segment.id, customer_id=customer.id, source="manual"))
        customers.append(customer)
    db.commit()
    return segment, customers


def _rows(client, auth_headers, segment) -> list[dict]:
    response = client.get(
        f"/api/v1/segments/{segment.id}/export.csv", headers=auth_headers
    )
    assert response.status_code == 200
    # The BOM is for Excel's benefit; utf-8-sig consumes it the way a CSV
    # reader should, and its presence is asserted separately below.
    text = response.content.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


def test_the_export_has_a_phone_column_beside_email(
    client, auth_headers, segment_with_customers
):
    """Test 8: the new column is there, and nothing else moved.

    The column list is asserted in full rather than just checking phone
    exists, because a downloaded file is somebody's spreadsheet — dropping or
    reordering a column silently breaks formulas built on top of it.
    """
    segment, _ = segment_with_customers
    rows = _rows(client, auth_headers, segment)

    assert list(rows[0].keys()) == [
        "external_id",
        "first_name",
        "last_name",
        "email",
        "phone",
        "city",
        "lifecycle_stage",
        "completed_orders",
        "lifetime_revenue",
        "days_since_last_order",
        "churn_score",
        "churn_risk_band",
        "rfm_segment",
        "recommended_action",
        "marketing_consent",
    ]


def test_every_exported_number_is_international_or_empty(
    client, auth_headers, segment_with_customers
):
    """Test 9: the column is one shape, all the way down.

    A column that is +64 for most rows and something else for the rest is the
    thing that makes somebody stop trusting the file.
    """
    segment, _ = segment_with_customers
    rows = _rows(client, auth_headers, segment)
    by_name = {row["first_name"]: row for row in rows}

    assert by_name["Sam"]["phone"] == "+642902076762"
    assert by_name["John"]["phone"] == "+64211234567"
    assert by_name["Nina"]["phone"] == ""
    assert by_name["Lou"]["phone"] == ""

    for row in rows:
        assert row["phone"] == "" or row["phone"].startswith("+"), row["phone"]
        assert "None" not in row["phone"]


def test_the_export_does_not_change_what_is_stored(
    db, client, auth_headers, segment_with_customers
):
    """Test 10: exporting is a read.

    The international form is how the number is *written down*, not a
    correction to the record. Rewriting the customer's row as a side effect of
    somebody clicking Export would be a change nobody asked for and nobody
    would see.
    """
    segment, customers = segment_with_customers
    before = {c.id: c.phone for c in customers}

    _rows(client, auth_headers, segment)

    db.expire_all()
    for customer in customers:
        refreshed = db.get(Customer, customer.id)
        assert refreshed.phone == before[customer.id], (
            "the export rewrote the stored phone number"
        )
    assert db.get(Customer, customers[0].id).phone == "02902076762"


def test_the_acceptance_example_verbatim(client, auth_headers, segment_with_customers):
    """Section 14, as a row rather than as prose."""
    segment, _ = segment_with_customers
    row = next(r for r in _rows(client, auth_headers, segment) if r["first_name"] == "Sam")

    assert row["first_name"] == "Sam"
    assert row["last_name"] == "Test"
    assert row["email"] == "sam@test.com"
    assert row["phone"] == "+642902076762"


def test_the_file_opens_as_utf8_in_a_spreadsheet(client, auth_headers, segment_with_customers):
    """A BOM, so Excel does not guess at a codepage.

    Not about phones, but it is the same download: a name with a macron in it
    arriving as mojibake is the other way this file loses somebody's trust.
    """
    segment, _ = segment_with_customers
    response = client.get(f"/api/v1/segments/{segment.id}/export.csv", headers=auth_headers)

    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "charset=utf-8" in response.headers["content-type"]
