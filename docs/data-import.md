# Importing data

Two ways in, one set of rules: a CSV upload from **Data & imports**, and the
authenticated ingestion API. Both funnel through the same row-level validators
in `app/services/ingestion.py`, so a file and an API call accept exactly the
same data and produce exactly the same errors.

Rows are validated independently. One bad row never rejects a file — it is
reported with its row number and reason, and everything else imports.

---

## Column names

Your columns do not have to be spelled the way this system spells them. Every
header is mapped to an internal field once, before any of the validators see
it, so a file written by hand and one exported by a developer import the same
way.

Two steps, in this order:

1. **Word breaks and case are normalised.** `First Name`, `FirstName` and
   ` FIRST_NAME ` are all `first_name`; `Total Amount` is `total_amount`.
2. **Genuine renames are looked up in a table.** `Item Name`, `Product` and
   `Product Name` all mean `product_name`; `Order Date` means `ordered_at`;
   `Customer ID` means `customer_external_id`; `Mobile` means `phone`.

A header matching neither keeps its own slug and no ingestor reads it. That is
deliberate: a column nobody recognises is ignored rather than guessed at,
because mapping `Delivery Notes` onto the nearest-looking field is how somebody
ends up with an address in their name.

A file carrying a single `Customer Name` column and no first/last is split on
the first space — but only when there is no first-name column of its own, since
guessing over the top of real data replaces it with a worse split.

The preview returns `column_mapping`, saying what each of your columns was read
as. Worth looking at before importing: "we read your 'Item Name' column as
product_name" is the difference between trusting an import and finding out a
month later that every product was blank.

This is the same set of names the message merge tags use, so a column that maps
to `first_name` is what `#first_name#` fills. Every tag names the column it
reads — see [the table in docs/api.md](api.md#personalisation-merge-tags) —
and a test asserts that every column in the upload template either has a tag
or has a recorded reason for not having one, so the two cannot drift apart.

## Timestamps

A cell reading `2026-09-16 19:40:00` says nothing about which clock wrote it,
and the database stores naive UTC. If your export records the **local** time an
order was placed — which most do — set `IMPORT_TIMESTAMPS_ARE_LOCAL=true`, or
a 7:40pm New Zealand order is stored as 19:40 UTC and read back as 7:40am the
next morning. That hour is what the learned routine, Smart Reorder's send time
and `#preferred_order_time#` are all built on, so the whole feature is half a
day out.

It defaults to `false`, which leaves existing data alone: flipping it changes
what an already-imported timestamp means, so it is a decision about a
particular export rather than something to guess. It applies to rows imported
after the change, so correcting it means re-importing. A timestamp that carries
its own offset (`2026-09-16T19:40:00+12:00`) is honoured either way.

---

## Preview is a real dry run

Uploading a file runs the **actual ingestor over every row** and throws the
result away. The counts and the error list are not a second implementation of
the rules that could drift from what the import does; they are the rules,
executed. So a preview can tell you things a header check cannot: a duplicate
`external_id` inside the file, a date in an unexpected format, a landline where
a mobile was expected.

Nothing is written. That is worth stating precisely, because every ingestor
commits when it finishes — running one and rolling back afterwards would import
the file. The dry run instead uses a session whose `commit` flushes rather than
persists (`_DryRunSession`), so the ingestor still sees its own writes and
in-file duplicate detection works, while nothing can reach the database. There
is no code path in it that can persist, which is what makes the guarantee
structural rather than a promise about the current ingestors.

The preview separates two things:

- **Rejected** — rows that will not import, with the reason. Absent and obvious.
- **Imported with a change** — rows that will import, but not exactly as
  supplied. Easy to miss, which is precisely why they are shown: a row that
  landed with a value dropped looks fine until somebody asks why that customer
  never gets texted.

---

## Phone numbers

Numbers are canonicalised to E.164 on the way in, because the SMS provider is
handed one string per recipient and is not the place to be guessing. All of
these are the same person and all are stored as `+64211234567`:

```
021 123 4567    0211234567    (021) 123-4567
+64 21 123 4567 0064211234567 64211234567    211234567
```

Anything that cannot be resolved with confidence is **refused rather than
guessed at** — a landline, a freephone number, a short code, a malformed
string. A wrong number is a text to a stranger.

What happens then depends on what else the row has:

| Row | Result |
| --- | --- |
| Unusable phone, has an email | Imports on email only. The number is dropped and reported as a change. |
| Unusable phone, no email | Rejected — nobody we can contact. |
| Usable phone in any shape | Imports, rewritten to `+64…`. Counted as a change. |

A change is only reported when the row actually lands. A row rejected later for
an unrelated field does not claim a rewrite that never happens.

---

## What a rejection looks like

Every rejected row carries its row number, the reason, and the identifying
columns — never the full row, which would put PII in an error report.

```
Row 5  (GD-1005)  'phone' value '0800 838 383' is not a mobile number an SMS
                  can reach, and there is no email address either.
Row 7  (GD-1007)  'email' value 'not-an-email' is not a valid email address.
Row 8  (GD-1001)  Duplicate external_id 'GD-1001' within this file.
Row 9  (GD-1009)  'signup_date' value 'not-a-date' is not a recognised date.
```

The same list downloads as a CSV from the import history, so a file can be
fixed and just those rows re-uploaded.

---

## Entity types and required columns

| Entity | Required |
| --- | --- |
| `customers` | `external_id`, plus an `email` or a `phone` |
| `orders` | `external_id`, `customer_external_id`, `ordered_at`, `total_amount` |
| `order_items` | `order_external_id`, `sku`, `quantity`, `unit_price` |
| `events` | `customer_external_id`, `event_type`, `occurred_at` |
| `consent_events` | `customer_external_id`, `consent_type`, `granted` |

Templates for each download from the upload card.

Importing customers or orders recomputes the affected customers' intelligence —
churn score, RFM, lifecycle stage — so segments reflect the new data without a
separate step.
