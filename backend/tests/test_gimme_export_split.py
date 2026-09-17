"""Reading a brand and a category out of a product name.

The GIMME export has neither, and without them preferred_brands and
preferred_categories compute empty for every customer — no Beer Drinkers
segment, no favourite-brand campaign, no category recommendation. So the
reading happens in the converter, and these pin the parts of it that are easy
to break without noticing.

The rule worth protecting: several houses here sell across two categories, and
only the wording of the pack separates them.
"""
from __future__ import annotations

import pytest

from scripts.split_gimme_export import classify, product_sku


@pytest.mark.parametrize(
    "name,brand,category",
    [
        # A bourbon house and its premixed cans.
        ("Jim Beam 1 Ltr", "Jim Beam", "Spirits"),
        ("Jim Beam Cola 10 Pk Btls", "Jim Beam", "RTDs"),
        # A vodka house and its premixed cans.
        ("Smirnoff Red 37.5% 1Ltr", "Smirnoff", "Spirits"),
        ("Smirnoff Guarana 12pk Can", "Smirnoff", "RTDs"),
        # A brewery that also puts its name on a vodka soda.
        ("Tui 12pk Cans", "Tui", "Beer"),
        ("Tui Vodka & Cherry Soda 7% 12 x 250mL", "Tui", "RTDs"),
        # Single-category houses are taken at their word.
        ("Heineken 24x330ml Btl", "Heineken", "Beer"),
        ("Fat Bird Sauv Blanc 750ml", "Fat Bird", "Wine"),
        ("Old Mout 1.5 Ltr", "Old Mout", "Cider"),
        ("Pals Vodka Lime & Soda 10x330 Cans", "Pals", "RTDs"),
        ("Sprite 1.5 Ltr", "Sprite", "Mixers"),
    ],
)
def test_a_product_is_read_as_the_shelf_it_came_off(name, brand, category):
    assert classify(name) == (brand, category)


def test_a_strong_lager_is_not_mistaken_for_a_premix():
    """7% marks a premix in this catalogue, but not on a beer brand.

    Kingfisher and NZ Lager are sold at 7% and are beer. Reading the ABV
    without first trusting a known brewery filed both as RTDs, which is the
    kind of wrong that shows up as a customer being sent RTD offers forever.
    """
    assert classify("Kingfisher 7% 500ml Can") == ("Kingfisher", "Beer")
    assert classify("Nz Lager 7% 500ml Can") == ("NZ Lager", "Beer")


def test_the_abv_on_a_spirit_bottle_is_not_an_rtd_marker():
    """37.5% contains "5%", and a naive match read it as a premix.

    Gordons rather than a single-category house on purpose: a brand that only
    ever sells one thing never reaches the wording rules, so testing this on
    one would pass without the guard being present at all.
    """
    assert classify("Gordons Pink Gin 37.5% 1 Ltr") == ("Gordons", "Spirits")
    assert classify("Gordons Pink Gin & Soda 12pk cans") == ("Gordons", "RTDs")


def test_an_unrecognised_product_is_left_blank_rather_than_guessed():
    """A guessed brand becomes a message about a drink they have never bought.

    Blank costs the item its vote on preference and nothing else — it still
    counts toward order totals, revenue and reorder timing.
    """
    assert classify("Assorted Party Pack") == ("", "")


def test_the_same_product_keeps_one_sku_across_orders():
    """The export's order_item_id is unique per line.

    Using it as the SKU made every purchase look like a different product, so
    nothing was ever bought twice and no product could be recommended.
    """
    assert product_sku("Fat Bird 750ml") == product_sku("  fat bird 750ml  ")
    assert product_sku("Fat Bird 750ml") != product_sku("Fat Bird Pinot Gris 750ml")
