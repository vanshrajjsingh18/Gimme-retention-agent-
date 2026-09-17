#!/usr/bin/env python3
"""Turn a GIMME "customers + order data till date" export into one import file.

The export is one row per order line, with the customer repeated on every row,
which is exactly the shape the importer's combined format takes. So this is a
translation of column names and nothing more structural than that.

The part that earns this script's existence is `category` and `brand`. They are
not in the export, they cannot be derived by the importer, and without them
`preferred_categories` and `preferred_brands` compute empty for every customer:
no Beer Drinkers segment, no favourite-brand campaign, no category
recommendation. They have to be read out of the product name here.

That reading is deliberately conservative. A product whose brand is not
recognised is written with a blank brand rather than a guessed one, because a
wrong brand does not stay in a spreadsheet — it becomes a message to a real
customer about a drink they have never bought. Unmatched items still count
toward order totals, revenue and reorder timing; they just do not vote on
preference.

    python -m scripts.split_gimme_export export.csv --out gimme.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

#: Brands, and the category a brand sells unless the product name says
#: otherwise. Ordered longest-first at match time so "Jim Beam Cola" is not
#: claimed by a shorter brand that happens to be a substring.
BRANDS: dict[str, tuple[str, str]] = {
    # spelling in the data          canonical brand      default category
    "pals": ("Pals", "RTDs"),
    "codys": ("Codys", "RTDs"),
    "cody's": ("Codys", "RTDs"),
    "kgb": ("KGB", "RTDs"),
    "cruiser": ("Cruiser", "RTDs"),
    "billy maverick": ("Billy Maverick", "RTDs"),
    "kirin hyoketsu": ("Kirin", "RTDs"),
    "kirin": ("Kirin", "RTDs"),
    "malibu": ("Malibu", "Spirits"),
    "long white": ("Long White", "RTDs"),
    "part time rangers": ("Part Time Rangers", "RTDs"),
    "ranfurly": ("Ranfurly", "Beer"),
    "woodstock": ("Woodstock", "RTDs"),
    "jim beam": ("Jim Beam", "Spirits"),
    "jack daniel": ("Jack Daniel's", "Spirits"),
    "canadian club": ("Canadian Club", "Spirits"),
    "jameson": ("Jameson", "Spirits"),
    "smirnoff": ("Smirnoff", "Spirits"),
    "absolut": ("Absolut", "Spirits"),
    "ivanov": ("Ivanov", "Spirits"),
    "stil": ("Stil", "Spirits"),
    "gordons": ("Gordons", "Spirits"),
    "gordon's": ("Gordons", "Spirits"),
    "bombay": ("Bombay", "Spirits"),
    "tanqueray": ("Tanqueray", "Spirits"),
    "bacardi": ("Bacardi", "Spirits"),
    "jose cuervo": ("Jose Cuervo", "Spirits"),
    "johnnie walker": ("Johnnie Walker", "Spirits"),
    "chivas": ("Chivas", "Spirits"),
    "famous grouse": ("Famous Grouse", "Spirits"),
    "teachers": ("Teachers", "Spirits"),
    "grants": ("Grants", "Spirits"),
    "jinro": ("Jinro", "Spirits"),
    "sailor jerry": ("Sailor Jerry", "Spirits"),
    "captain morgan": ("Captain Morgan", "Spirits"),
    "russian standard": ("Russian Standard", "Spirits"),
    "42 below": ("42 Below", "Spirits"),
    "broken shed": ("Broken Shed", "Spirits"),
    # beer
    "nz lager": ("NZ Lager", "Beer"),
    "kingfisher": ("Kingfisher", "Beer"),
    "victoria bitter": ("Victoria Bitter", "Beer"),
    "heineken": ("Heineken", "Beer"),
    "steinlager": ("Steinlager", "Beer"),
    "speight": ("Speight's", "Beer"),
    "corona": ("Corona", "Beer"),
    "foster": ("Fosters", "Beer"),
    "flame": ("Flame", "Beer"),
    "export gold": ("Export Gold", "Beer"),
    "export citrus": ("Export", "Beer"),
    "red horse": ("Red Horse", "Beer"),
    "double brown": ("Double Brown", "Beer"),
    "lion red": ("Lion Red", "Beer"),
    "waikato": ("Waikato", "Beer"),
    "stella artois": ("Stella Artois", "Beer"),
    "asahi": ("Asahi", "Beer"),
    "tiger": ("Tiger", "Beer"),
    "peroni": ("Peroni", "Beer"),
    "becks": ("Becks", "Beer"),
    "carlsberg": ("Carlsberg", "Beer"),
    "monteith": ("Monteith's", "Beer"),
    "panhead": ("Panhead", "Beer"),
    "garage project": ("Garage Project", "Beer"),
    "emerson": ("Emerson's", "Beer"),
    "macs": ("Macs", "Beer"),
    "sol ": ("Sol", "Beer"),
    "tui": ("Tui", "Beer"),
    "db draught": ("DB Draught", "Beer"),
    "haagen": ("Haagen", "Beer"),
    "summit": ("Speight's", "Beer"),
    # wine
    "fat bird": ("Fat Bird", "Wine"),
    "shingle peak": ("Shingle Peak", "Wine"),
    "young & co": ("Young & Co", "Wine"),
    "jacob": ("Jacob's Creek", "Wine"),
    "wither hills": ("Wither Hills", "Wine"),
    "waipara hills": ("Waipara Hills", "Wine"),
    "banrock": ("Banrock Station", "Wine"),
    "white cliff": ("White Cliff", "Wine"),
    "whitecliff": ("White Cliff", "Wine"),
    "haha": ("Haha", "Wine"),
    "villa maria": ("Villa Maria", "Wine"),
    "oyster bay": ("Oyster Bay", "Wine"),
    "stoneleigh": ("Stoneleigh", "Wine"),
    "brancott": ("Brancott", "Wine"),
    "lindauer": ("Lindauer", "Wine"),
    "veuve": ("Veuve Clicquot", "Wine"),
    "moet": ("Moet", "Wine"),
    "selaks": ("Selaks", "Wine"),
    "mud house": ("Mud House", "Wine"),
    "squealing pig": ("Squealing Pig", "Wine"),
    "matua": ("Matua", "Wine"),
    "giesen": ("Giesen", "Wine"),
    "seresin": ("Seresin", "Wine"),
    # cider
    "old mout": ("Old Mout", "Cider"),
    "scrumpy": ("Scrumpy", "Cider"),
    "rekorderlig": ("Rekorderlig", "Cider"),
    "somersby": ("Somersby", "Cider"),
    "isaac": ("Isaac's", "Cider"),
    # How the export abbreviates a house it has already spelled out elsewhere.
    "jd double jack": ("Jack Daniel's", "RTDs"),
    "jd cola": ("Jack Daniel's", "RTDs"),
    "gentleman jack": ("Jack Daniel's", "Spirits"),
    "jb gold": ("Jim Beam", "RTDs"),
    "jb ": ("Jim Beam", "RTDs"),
    "cc dry": ("Canadian Club", "RTDs"),
    "lw apple": ("Long White", "RTDs"),
    "lw ": ("Long White", "RTDs"),
    # More of the catalogue.
    "export": ("Export", "Beer"),
    "wakachangi": ("Wakachangi", "Beer"),
    "vailima": ("Vailima", "Beer"),
    "stella": ("Stella Artois", "Beer"),
    "ice beer": ("Ice Beer", "Beer"),
    "odd company": ("Odd Company", "RTDs"),
    "odd com": ("Odd Company", "RTDs"),
    "barrel": ("Barrel", "RTDs"),
    "black heart": ("Black Heart", "RTDs"),
    "nitro": ("Nitro", "RTDs"),
    "kristov": ("Kristov", "Spirits"),
    "finlandia": ("Finlandia", "Spirits"),
    "riverstone": ("Riverstone", "Spirits"),
    "hardy": ("Hardy's", "Wine"),
    "bay & barnes": ("Bay & Barnes", "Wine"),
    "wairau river": ("Wairau River", "Wine"),
    "gunn est": ("Gunn Estate", "Wine"),
    "country chasseur": ("Country Chasseur", "Wine"),
    # Non-alcohol. Real purchases, and they belong in a category of their own
    # rather than skewing which drink somebody is thought to prefer.
    "coca-cola": ("Coca-Cola", "Mixers"),
    "coca cola": ("Coca-Cola", "Mixers"),
    "coke": ("Coca-Cola", "Mixers"),
    "sprite": ("Sprite", "Mixers"),
    "fanta": ("Fanta", "Mixers"),
    "pepsi": ("Pepsi", "Mixers"),
    "schweppes": ("Schweppes", "Mixers"),
    "l&p": ("L&P", "Mixers"),
    "red bull": ("Red Bull", "Mixers"),
    "party ice": ("Party Ice", "Mixers"),
}

#: Wording that settles the category regardless of what the brand usually
#: sells. A bourbon house also sells premixed cans; a vodka house also sells
#: a bottle of vodka. The pack tells them apart.
#: An ABV in the 5-9% band is how this catalogue marks a premix. The lookbehind
#: keeps it off the 37.5% on a bottle of vodka. "Cans" is deliberately not a cue
#: — beer comes in cans too, and using it filed Tui's lager as an RTD.
RTD_CUES = re.compile(
    r"(?<![\d.])[5-9](?:\.\d)?\s*%"
    # No \b before & or + — a boundary needs a word/non-word transition, and
    # the space before "& Soda" is non-word already, so \b& never matches.
    r"|(?:&|\+|\band\b)\s*(?:cola|dry|soda|lime|lemon|cranberry|guarana|energy|tonic)"
    r"|\bcola\b|\bpremix|\brtd\b|\bguarana\b|\bvodka\s+(?:soda|lime)\b",
    re.IGNORECASE,
)
SPIRIT_CUES = re.compile(
    r"\b(?:700|750)\s*ml\b|\b1\s*(?:ltr|litre|liter|l)\b|\b1125\s*ml\b|\bbottle\s*$",
    re.IGNORECASE,
)
WINE_CUES = re.compile(
    r"\bsauv(?:ignon)?\b|\bblanc\b|\bpinot\b|\bros[eé]\b|\bchardon(?:nay)?\b"
    r"|\bmerlot\b|\bsyrah\b|\bshiraz\b|\briesling\b|\bgris\b|\bsparkling\b"
    r"|\bchampagne\b|\bprosecco\b|\bmoscato\b|\bcabernet\b",
    re.IGNORECASE,
)
CIDER_CUES = re.compile(r"\bcider\b|\bapple\b|\bfeijoa\b", re.IGNORECASE)
BEER_CUES = re.compile(r"\blager\b|\bpilsner\b|\bipa\b|\bale\b|\bstout\b|\bbitter\b", re.IGNORECASE)

#: Brands whose default category is overridden by the cues above rather than
#: trusted. These sell across two categories in this catalogue.
DUAL_CATEGORY = {
    "Jim Beam", "Jack Daniel's", "Canadian Club", "Jameson", "Smirnoff",
    "Woodstock", "Malibu", "Gordons", "Tui", "Bacardi", "Captain Morgan",
}

_BRAND_KEYS = sorted(BRANDS, key=len, reverse=True)
_SLUG = re.compile(r"[^a-z0-9]+")


def classify(item_name: str) -> tuple[str, str]:
    """Return (brand, category) for a product name, blank where unsure."""
    text = item_name.lower()
    brand = category = ""

    for key in _BRAND_KEYS:
        if key in text:
            brand, category = BRANDS[key]
            break

    # Wording beats the brand's usual shelf for anyone who sells across two.
    if not brand or brand in DUAL_CATEGORY:
        if WINE_CUES.search(item_name):
            category = "Wine"
        elif CIDER_CUES.search(item_name):
            category = "Cider"
        elif SPIRIT_CUES.search(item_name) and not RTD_CUES.search(item_name):
            category = "Spirits" if brand else category
        elif RTD_CUES.search(item_name) and brand:
            category = "RTDs"
        elif BEER_CUES.search(item_name):
            category = "Beer"

    return brand, category


def product_sku(item_name: str) -> str:
    """A stable id for the same product across orders.

    The export's order_item_id is unique per line, so using it as the SKU makes
    every purchase look like a different product and nothing ever appears to be
    bought twice. Slugging the name is coarse — two spellings of one drink stay
    apart — but it at least groups the repeats.
    """
    return _SLUG.sub("-", item_name.strip().lower()).strip("-")[:64] or "unknown"


def parse_when(value: str) -> str:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).isoformat()
        except ValueError:
            continue
    return ""


def convert(path: Path) -> tuple[list[dict], Counter]:
    """Read the export into the importer's combined shape: one row per line."""
    rows: list[dict] = []
    seen_items: set[str] = set()
    stats: Counter = Counter()

    with path.open(encoding="utf-8-sig", newline="") as handle:
        for source in csv.DictReader(handle):
            fan_id = (source.get("fan_id") or "").strip()
            order_id = (source.get("order_id") or "").strip()
            if not fan_id or not order_id:
                stats["rows_without_ids"] += 1
                continue

            item_id = (source.get("order_item_id") or "").strip()
            if item_id and item_id in seen_items:
                continue
            if item_id:
                seen_items.add(item_id)

            phone = (source.get("phone") or "").strip()
            # The export writes a bare "+" where it holds no number.
            if phone in {"+", "-"}:
                phone = ""
            parts = (source.get("customer_name") or "").strip().split()

            name = (source.get("item_name") or "").strip()
            brand, category = classify(name)
            stats["items"] += 1
            stats["with_brand" if brand else "without_brand"] += 1
            stats["with_category" if category else "without_category"] += 1

            rows.append({
                "customer_external_id": fan_id,
                "email": (source.get("email") or "").strip(),
                "phone": phone,
                "first_name": parts[0] if parts else "",
                "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
                "country": "New Zealand",
                "order_external_id": order_id,
                "ordered_at": parse_when(source.get("order_date") or ""),
                "status": "COMPLETED",
                "total_amount": (source.get("order_amount") or "0").strip(),
                "discount_amount": _first_number(source.get("discount_value")),
                "currency": "NZD",
                "coupon_code": _first_text(source.get("discount_code")),
                "item_external_id": item_id,
                "sku": product_sku(name),
                "product_name": name,
                "category": category,
                "brand": brand,
                "quantity": (source.get("quantity") or "1").strip() or "1",
                "unit_price": (source.get("item_price") or "0").strip() or "0",
                "line_total": (source.get("item_price") or "0").strip() or "0",
            })

    return rows, stats


def _first_number(value: str | None) -> str:
    """Pull 4.99 out of the export's `[4.99]`."""
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return match.group(0) if match else "0"


def _first_text(value: str | None) -> str:
    """Pull ELITE15 out of the export's `["ELITE15"]`."""
    match = re.search(r'"([^"]+)"', value or "")
    return match.group(1) if match else ""


COLUMNS = [
    "customer_external_id", "email", "phone", "first_name", "last_name", "country",
    "marketing_consent", "email_consent", "sms_consent",
    "order_external_id", "ordered_at", "status", "total_amount", "discount_amount",
    "currency", "coupon_code",
    "item_external_id", "sku", "product_name", "category", "brand",
    "quantity", "unit_price", "line_total",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="the GIMME export CSV")
    parser.add_argument("--out", type=Path, default=Path("gimme_combined.csv"))
    parser.add_argument(
        "--assume-consent",
        action="store_true",
        help=(
            "Write marketing consent as granted for everyone, and each channel "
            "as granted where there is an address to reach it on. The export "
            "carries no consent, so this asserts something the data does not "
            "say. Off by default: it should be a decision somebody made, not a "
            "default nobody saw."
        ),
    )
    args = parser.parse_args()

    if not args.export.is_file():
        print(f"No such file: {args.export}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)

    rows, stats = convert(args.export)
    columns = list(COLUMNS)
    if args.assume_consent:
        for row in rows:
            # Consent to be emailed is worth nothing without an address to
            # email, and claiming it hides why somebody is never contacted.
            row["marketing_consent"] = "true"
            row["email_consent"] = "true" if row["email"] else "false"
            row["sms_consent"] = "true" if row["phone"] else "false"
    else:
        for name in ("marketing_consent", "email_consent", "sms_consent"):
            columns.remove(name)

    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    customers = len({r["customer_external_id"] for r in rows})
    orders = len({r["order_external_id"] for r in rows})
    print(f"{args.out}")
    print(f"  {len(rows)} rows — {customers} customers, {orders} orders")
    matched = stats["with_brand"]
    print(
        f"  brand recognised on {matched} of {stats['items']} lines "
        f"({matched / max(stats['items'], 1):.0%}); category on {stats['with_category']}."
    )
    print(
        "\nUnrecognised lines keep a blank brand rather than a guessed one. They "
        "still count toward totals and reorder timing; they do not vote on which "
        "brand a customer prefers."
    )
    if args.assume_consent:
        print(
            "\nConsent was written as granted for everyone. The export does not "
            "say that — you did. Worth being able to point at where it was "
            "actually collected before anything sends."
        )
    else:
        print(
            "\nNo consent columns: every customer will load contactable on "
            "nothing and every campaign will skip them. Add marketing_consent, "
            "email_consent and sms_consent from wherever consent was collected, "
            "or re-run with --assume-consent."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
