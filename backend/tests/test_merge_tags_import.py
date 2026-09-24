"""From a spreadsheet column to a merge tag, end to end.

The two halves of this feature are joined at a name. The composer offers
`#first_name#`, the resolver looks up `first_name`, and both are useless if
the importer dropped the column because GIMME's export happens to call it
"First Name". That failure is quiet in the worst way: the import reports
every row accepted, the customers are all there, and every message goes out
greeting somebody as "there".

So these tests start from a file with the column spellings a real export
uses, and end at a rendered message.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.enums import CampaignCopyMode, CampaignStatus, Channel, RecipientStatus
from app.models.base import utcnow
from app.models.entities import Campaign, CampaignRecipient, Customer, Message
from app.core.timezones import to_utc_naive
from app.services.ingestion import canonical_header, ingest_csv
from app.services.intelligence import refresh_customer
from app.services.merge_tags import resolve_message_template

#: The column names in the header row are deliberately all different from the
#: internal ones: spaced, camelCase, and renamed outright.
FILE = """Customer ID,First Name,Last Name,Email Address,Mobile,Order ID,Order Date,Order Status,Order Total,Item Name,Product Category,Product Brand,Qty,Item Price
{cid},Sam,Rangi,sam@example.test,+64211239876,{cid}-O1,2026-08-05 19:32:00,COMPLETED,54.00,Corona Extra,Beer,Corona,6,9.00
{cid},Sam,Rangi,sam@example.test,+64211239876,{cid}-O2,2026-08-12 19:41:00,COMPLETED,54.00,Corona Extra,Beer,Corona,6,9.00
{cid},Sam,Rangi,sam@example.test,+64211239876,{cid}-O3,2026-08-19 19:36:00,COMPLETED,61.00,Corona Extra,Beer,Corona,6,9.00
"""


@pytest.fixture()
def imported(db, bootstrapped) -> Customer:
    """A customer loaded from a file that uses none of our column names."""
    cid = f"SAM-{int(utcnow().timestamp() * 1000)}"
    job = ingest_csv(db, "combined", FILE.format(cid=cid).encode(), filename="export.csv")
    assert job.rejected_rows == 0, job.errors

    customer = db.execute(
        select(Customer).where(Customer.external_id == cid)
    ).scalars().one()
    refresh_customer(db, customer)
    db.refresh(customer)
    return customer


# ==========================================================================
# The mapping itself
# ==========================================================================
@pytest.mark.parametrize(
    "column,field",
    [
        ("First Name", "first_name"),
        ("FirstName", "first_name"),
        ("first_name", "first_name"),
        ("Customer First Name", "first_name"),
        ("customer_first_name", "first_name"),
        ("  FIRST NAME  ", "first_name"),
        ("Product", "product_name"),
        ("Product Name", "product_name"),
        ("product_name", "product_name"),
        ("Item Name", "product_name"),
        ("ItemName", "product_name"),
    ],
)
def test_a_column_is_read_by_what_it_means_not_how_it_is_spelled(column, field):
    """The spellings from the brief, and the ones a developer's export uses.

    Two rules, in order: word breaks and case are normalised, then a genuine
    rename is looked up. "Item Name" is not a formatting difference from
    "product_name" — it is a different word, and it needs the table.
    """
    assert canonical_header(column) == field


def test_a_column_nobody_recognises_is_ignored_rather_than_guessed_at():
    """The line this stops short of.

    Mapping an unknown column onto the nearest-looking field is how somebody's
    "Delivery Notes" ends up in the customer's name. An unrecognised header
    keeps its own slug and no ingestor reads it.
    """
    assert canonical_header("Delivery Notes") == "delivery_notes"
    assert canonical_header("Loyalty Tier") == "loyalty_tier"


def test_the_preview_says_how_each_column_was_read(client, auth_headers):
    """A header check that only names what is missing is no help.

    Somebody looking at a file whose first column is plainly called "First
    Name" needs to be told what we made of it, not that first_name is absent.
    """
    from app.services.ingestion import preview_csv

    preview = preview_csv("combined", FILE.format(cid="PREVIEW-1").encode())
    mapping = {entry["column"]: entry["field"] for entry in preview["column_mapping"]}

    assert mapping["First Name"] == "first_name"
    assert mapping["Item Name"] == "product_name"
    assert mapping["Order Total"] == "total_amount"
    assert preview["missing_required_columns"] == []


# ==========================================================================
# Through to a rendered message
# ==========================================================================
def test_an_imported_file_resolves_its_merge_tags(db, imported):
    """Test 12 from the brief, run the whole way rather than at the seam.

    Every value here came out of a column named something else in the file.
    """
    resolved = resolve_message_template(
        db,
        "Hi #first_name# #last_name#, fancy another #product#? "
        "That is your #category# from #brand#, #email#.",
        imported.id,
    )
    assert resolved.text == (
        "Hi Sam Rangi, fancy another Corona Extra? "
        "That is your Beer from Corona, sam@example.test."
    )
    assert not resolved.missing_fields
    assert not resolved.unknown_tags


def test_the_brief_example_renders_as_written(db, imported):
    """Section 17, verbatim."""
    resolved = resolve_message_template(
        db,
        "Hi #first_name#, it's been a little while. Fancy another #product#? "
        "GIMME is ready when you are.",
        imported.id,
    )
    assert resolved.text == (
        "Hi Sam, it's been a little while. Fancy another Corona Extra? "
        "GIMME is ready when you are."
    )


def test_product_follows_the_latest_order_not_the_longest_habit(db, imported):
    """A customer who has just switched should be asked about the new thing.

    Their most-ordered product is the fallback, not the answer: three Corona
    orders and one Heineken last week means "fancy another Heineken?".
    """
    from app.models.entities import Order, OrderItem

    order = Order(
        external_id=f"{imported.external_id}-O4",
        customer_id=imported.id,
        ordered_at=utcnow() - timedelta(days=1),
        status="COMPLETED",
        total_amount=48.0,
    )
    db.add(order)
    db.flush()
    db.add(
        OrderItem(
            external_id=f"{imported.external_id}-I4",
            order_id=order.id,
            sku="heineken-12",
            product_name="Heineken 12pk",
            category="Beer",
            brand="Heineken",
            quantity=1,
            unit_price=48.0,
            line_total=48.0,
        )
    )
    db.commit()
    refresh_customer(db, imported)

    assert resolve_message_template(db, "#product#", imported.id).text == "Heineken 12pk"
    # And what they buy most is still what #preferred_brand# means.
    assert resolve_message_template(db, "#preferred_brand#", imported.id).text == "Corona"


def test_running_the_same_campaign_again_picks_up_the_new_data(db, imported):
    """Test 13 from the brief: the template is a question, not an answer.

    Nothing is cached between runs, and the stored copy is untouched by the
    first one — which is the only reason the second can say something
    different.
    """
    template = "Hi #first_name#, fancy another #product#?"
    # The file carries no consent columns, so the import loads them
    # uncontactable on purpose. Granting it here is what an operator does
    # before a send; without it the run correctly skips them and this test
    # would be asserting on nothing.
    imported.marketing_consent = True
    imported.sms_consent = True
    imported.age_verified = True
    db.commit()

    campaign = Campaign(
        name=f"Repeat {utcnow().timestamp()}",
        objective="REORDER",
        channel=Channel.SMS.value,
        status=CampaignStatus.APPROVED.value,
        body=template + " Reply STOP to opt out.",
        copy_mode=CampaignCopyMode.WRITTEN.value,
        compliance_result={"passed": True, "blocking_count": 0, "findings": []},
    )
    db.add(campaign)
    db.flush()
    db.add(
        CampaignRecipient(
            campaign_id=campaign.id,
            customer_id=imported.id,
            status=RecipientStatus.ELIGIBLE.value,
        )
    )
    db.commit()

    from app.campaigns.service import run_campaign

    # A fixed mid-morning send. Left to the wall clock, this test passes all
    # day and fails after 7pm, when quiet hours correctly hold the message.
    morning = to_utc_naive(datetime(2026, 9, 24, 11, 0))
    run_campaign(db, campaign, simulate_engagement=False, now=morning)
    first = _last_body(db, campaign, imported)
    assert "another Corona Extra?" in first

    # The customer's name changes in the source system, as names do.
    imported.first_name = "Samuel"
    db.commit()

    # A repeat send is the same audience row, queued again — that is what
    # makes this a second run of one campaign rather than a new campaign.
    campaign.status = CampaignStatus.APPROVED.value
    recipient = db.execute(
        select(CampaignRecipient).where(
            CampaignRecipient.campaign_id == campaign.id,
            CampaignRecipient.customer_id == imported.id,
        )
    ).scalars().one()
    recipient.status = RecipientStatus.ELIGIBLE.value
    db.commit()
    run_campaign(db, campaign, simulate_engagement=False, now=morning)

    assert _last_body(db, campaign, imported).startswith("Hi Samuel,")
    db.refresh(campaign)
    assert campaign.body.startswith(template), "the first run rewrote the template"


def _last_body(db, campaign, customer) -> str:
    return db.execute(
        select(Message.body)
        .where(Message.campaign_id == campaign.id, Message.customer_id == customer.id)
        .order_by(Message.id.desc())
    ).scalars().first()
