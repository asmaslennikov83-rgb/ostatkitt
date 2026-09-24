from app.models import ProductVariant
from app.price_compare import _match_variants, _seller_price_for_variant, _order_counts


def test_common_alternative_skus_are_single_product():
    left = ProductVariant("cabinet_1", 11, 100, ("111", "222", "333", "444"))
    right = ProductVariant("cabinet_2", 12, 200, ("222", "333", "444", "555"))
    pairs, ambiguous = _match_variants([{11: left}, {12: right}])
    assert pairs == [(left, right)]
    assert ambiguous == 0
    assert _order_counts([{"barcode": "111"}, {"barcode": "333"}],
                         {barcode: left for barcode in left.skus}, {11: left}) == {11: 2}


def test_only_common_goods_and_ambiguous_skus_are_excluded():
    a = ProductVariant("cabinet_1", 1, 1, ("a",))
    b = ProductVariant("cabinet_1", 2, 2, ("b",))
    c = ProductVariant("cabinet_2", 3, 3, ("a",))
    pairs, ambiguous = _match_variants([{1: a, 2: b}, {3: c}])
    assert pairs == [(a, c)] and ambiguous == 0


def test_size_price_wont_attribute_wrong_size():
    good = {"sizes": [{"sizeID": 22, "discountedPrice": 100},
                        {"sizeID": 23, "discountedPrice": 200}]}
    assert _seller_price_for_variant(good, 23) == 200
    assert _seller_price_for_variant(good, 99) is None
