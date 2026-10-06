from app.models import ProductVariant, Warehouse, DistributionLine
from app.service import (
    _build_sku_alias_groups, _apply_cabinet_barcode_exclusions,
    _expand_variants_for_aliases, _normalize_stock_by_alias_group,
    _build_output_sku_maps,
)


def test_06_oct_conflicting_aliases_keep_physical_stocks_separate():
    correct = '4600987012223'
    wrong = '4600987014272'
    other = '4600987014289'
    foreign = '4600987012193'
    cab1_correct = ProductVariant('cabinet_1', 101, 1, (correct, wrong, other))
    cab1_foreign = ProductVariant('cabinet_1', 102, 2, (foreign,))
    cab2_mixed = ProductVariant('cabinet_2', 201, 3, (foreign, correct))
    raw = {
        'cabinet_1': {correct: cab1_correct, wrong: cab1_correct,
                      other: cab1_correct, foreign: cab1_foreign},
        'cabinet_2': {foreign: cab2_mixed, correct: cab2_mixed},
    }
    root, groups = _build_sku_alias_groups(raw)
    assert root[correct] == root[wrong] == root[other]
    assert root[correct] != root[foreign], 'Different chrtIDs in cab1 must not merge'

    stock, excluded_roots, preferred, merged = _normalize_stock_by_alias_group(
        {correct: 37, foreign: 60}, set(), root,
    )
    assert stock == {correct: 37, foreign: 60}
    assert merged == {root[correct]: [correct], root[foreign]: [foreign]}

    filtered = _apply_cabinet_barcode_exclusions(raw, {'cabinet_1': {wrong}})
    expanded = _expand_variants_for_aliases(filtered, groups)
    assert expanded['cabinet_1'][correct].chrt_id == 101
    assert expanded['cabinet_1'][foreign].chrt_id == 102
    assert wrong not in filtered['cabinet_1']

    output, canonical = _build_output_sku_maps(
        filtered, groups, root, excluded_roots, preferred, set(),
    )
    assert correct in output['cabinet_1']
    assert wrong not in output['cabinet_1']
    assert canonical['cabinet_1'][correct] == correct
    assert canonical['cabinet_1'][wrong] == correct
    assert canonical['cabinet_1'][foreign] == foreign
    # Both physical barcodes refer to one WB variant in cabinet 2:
    # export only one canonical SKU there.
    assert len(output['cabinet_2']) == 1
    assert canonical['cabinet_2'][foreign] == canonical['cabinet_2'][correct]


def test_nonconflicting_aliases_still_merge():
    a, b = '111', '222'
    v1 = ProductVariant('cabinet_1', 11, 1, (a, b))
    v2 = ProductVariant('cabinet_2', 22, 2, (a, b))
    root, groups = _build_sku_alias_groups({
        'cabinet_1': {a: v1, b: v1},
        'cabinet_2': {a: v2, b: v2},
    })
    assert root[a] == root[b]
    stock, *_ = _normalize_stock_by_alias_group({a: 4, b: 6}, set(), root)
    assert sum(stock.values()) == 10
    assert len(stock) == 1
