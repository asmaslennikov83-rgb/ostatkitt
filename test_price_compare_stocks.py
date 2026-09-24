import asyncio
from pathlib import Path
from types import SimpleNamespace
from openpyxl import load_workbook
from app.price_compare import _load_fbo_stocks, _load_fbs_stocks, _export_report

class FakeClient:
    cabinet = SimpleNamespace(name='Test')
    async def _request(self, method, url, **kwargs):
        if 'wb-warehouses' in url:
            return {'data': {'items': [
                {'chrtId': 1, 'quantity': 8, 'inWayToClient': 3},
                {'chrtId': 1, 'quantity': 4, 'inWayFromClient': 2},
                {'chrtId': 2, 'quantity': 7}]}}
        wh = int(url.rsplit('/', 1)[-1])
        return {'stocks': [{'chrtId': 1, 'amount': wh}, {'chrtId': 2, 'amount': 0}]}
    async def get_warehouses(self):
        return [SimpleNamespace(warehouse_id=2), SimpleNamespace(warehouse_id=3)]

def test_stock_aggregation():
    client = FakeClient()
    assert asyncio.run(_load_fbo_stocks(client)) == {1: 12, 2: 7}
    assert asyncio.run(_load_fbs_stocks(client, {1: None, 2: None})) == {1: 5, 2: 0}

def test_columns_and_formulas(tmp_path: Path):
    path = tmp_path / 'report.xlsx'
    _export_report(path, [('123', 'article', 100, 10, 12, 5, None, 90, 15, 7, 3, None, None, None, '123', '123')],
                   ['cab1', 'cab2'], 7, 0, '2026-09-17', '2026-09-23')
    sheet = load_workbook(path).active
    assert sheet['E3'].value.startswith('Остатки FBO')
    assert sheet['J3'].value.startswith('Остатки FBO')
    assert sheet['G4'].value == '=E4+F4'
    assert sheet['L4'].value == '=J4+K4'
    assert 'I4-D4' in sheet['M4'].value
