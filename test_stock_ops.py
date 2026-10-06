from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.config import CabinetConfig, Settings
from app.models import ProductVariant, Warehouse
from app.stock_ops import StockManager, StockOperationError, load_distribution_targets
import app.stock_ops as stock_ops


def settings(tmp_path):
    return Settings('tg', {123}, (
        CabinetConfig('cabinet_1', 'TT', 'token1'),
        CabinetConfig('cabinet_2', 'TT Group', 'token2'),
    ), 14, 4, 2, 20, 4, base_dir=tmp_path, admin_ids={123})


class Session:
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False


class FakeWB:
    stock = {}
    puts = []
    drift = False
    def __init__(self, cabinet, session):
        self.cabinet = cabinet
    async def get_warehouses(self):
        return [Warehouse(self.cabinet.key, self.cabinet.name, 11, 'Main')]
    async def get_catalog_variants(self):
        variants = {
            101: ProductVariant(self.cabinet.key, 101, 1, ('sku1', 'alias1')),
            102: ProductVariant(self.cabinet.key, 102, 2, ('sku2',)),
        }
        return {sku: variant for variant in variants.values() for sku in variant.skus}, variants
    async def get_fbs_stocks(self, wh, chrt_ids):
        return {int(i): self.stock.get((self.cabinet.key, wh, int(i)), 0) for i in chrt_ids}
    async def put_fbs_stocks(self, wh, amounts):
        self.puts.append((self.cabinet.key, wh, dict(amounts)))
        for chrt, qty in amounts.items():
            self.stock[(self.cabinet.key, wh, chrt)] = qty


def make_excel(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(['ШК', 'Остаток'])
    for row in rows: ws.append(row)
    wb.save(path)


@pytest.fixture(autouse=True)
def reset_fake(monkeypatch):
    FakeWB.stock = {
        ('cabinet_1', 11, 101): 5,
        ('cabinet_1', 11, 102): 9,
        ('cabinet_2', 11, 101): 4,
        ('cabinet_2', 11, 102): 3,
    }
    FakeWB.puts = []
    monkeypatch.setattr(stock_ops, 'WBClient', FakeWB)
    monkeypatch.setattr(stock_ops.aiohttp, 'ClientSession', Session)


def test_update_preview_confirm_and_rollback(tmp_path):
    manager = StockManager(settings(tmp_path))
    files = []
    for name in ('TT', 'TT Group'):
        p = tmp_path / f'{name} — Main.xlsx'
        make_excel(p, [('sku1', 10), ('sku2', 0)])
        files.append(p)
    async def scenario():
        preview = await manager.preview('distribution', 123, {'cabinet_1', 'cabinet_2'}, files=files)
        assert len(FakeWB.puts) == 0
        assert preview['summary']['changed'] == 4
        assert preview['summary']['zeroed'] == 2
        result = await manager.execute(preview['id'], 123)
        assert result['status'] == 'verified'
        assert result['verified_count'] == 4
        assert FakeWB.stock[('cabinet_1', 11, 101)] == 10
        assert FakeWB.stock[('cabinet_2', 11, 102)] == 0
        assert manager.latest_rollback_source()['id'] == preview['id']
        rollback = await manager.preview('rollback', 123, {'cabinet_1', 'cabinet_2'}, source_id=preview['id'])
        assert rollback['summary']['changed'] == 4
        restored = await manager.execute(rollback['id'], 123)
        assert restored['status'] == 'verified'
        assert FakeWB.stock[('cabinet_1', 11, 101)] == 5
        assert FakeWB.stock[('cabinet_2', 11, 102)] == 3
        assert manager.latest_rollback_source() is None
    asyncio.run(scenario())


def test_zero_requires_phrase_and_restores(tmp_path):
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'})
        assert preview['summary']['zeroed'] == 2
        assert FakeWB.puts == []
        with pytest.raises(StockOperationError, match='ОБНУЛИТЬ'):
            await manager.execute(preview['id'], 123)
        manager.zero_authorized.add((123, preview['id']))
        result = await manager.execute(preview['id'], 123)
        assert result['status'] == 'verified'
        assert FakeWB.stock[('cabinet_1', 11, 101)] == 0
        assert FakeWB.stock[('cabinet_2', 11, 101)] == 4
        rollback = await manager.preview('rollback', 123, {'cabinet_1'}, source_id=preview['id'])
        assert (await manager.execute(rollback['id'], 123))['status'] == 'verified'
        assert FakeWB.stock[('cabinet_1', 11, 101)] == 5
    asyncio.run(scenario())


def test_stale_preview_prevents_any_put(tmp_path):
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'})
        manager.zero_authorized.add((123, preview['id']))
        FakeWB.stock[('cabinet_1', 11, 101)] = 4
        with pytest.raises(StockOperationError, match='изменились'):
            await manager.execute(preview['id'], 123)
        assert not FakeWB.puts
        assert manager.list_records()[0]['status'] == 'aborted'
    asyncio.run(scenario())


def test_rollback_skips_post_update_changes(tmp_path):
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'})
        manager.zero_authorized.add((123, preview['id']))
        await manager.execute(preview['id'], 123)
        FakeWB.stock[('cabinet_1', 11, 101)] = 2  # e.g. changed after zero
        rollback = await manager.preview('rollback', 123, {'cabinet_1'}, source_id=preview['id'])
        assert len(rollback['conflicts']) == 1
        assert rollback['summary']['changed'] == 1
        await manager.execute(rollback['id'], 123)
        assert FakeWB.stock[('cabinet_1', 11, 101)] == 2
        assert FakeWB.stock[('cabinet_1', 11, 102)] == 9
    asyncio.run(scenario())


def test_aliases_same_chrt_fail_closed(tmp_path):
    path = tmp_path / 'TT — Main.xlsx'
    make_excel(path, [('sku1', 10), ('alias1', 0)])
    wh = Warehouse('cabinet_1', 'TT', 11, 'Main')
    with pytest.raises(StockOperationError, match='Два ШК одного chrtID'):
        load_distribution_targets([path], {'cabinet_1:11': wh},
                                  {'cabinet_1': {'sku1': 101, 'alias1': 101}})


def test_rollback_can_restore_cabinets_separately(tmp_path):
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1', 'cabinet_2'})
        manager.zero_authorized.add((123, preview['id']))
        await manager.execute(preview['id'], 123)
        first = await manager.preview('rollback', 123, {'cabinet_1'}, source_id=preview['id'])
        await manager.execute(first['id'], 123)
        assert manager.latest_rollback_source()['id'] == preview['id']
        second = await manager.preview('rollback', 123, {'cabinet_2'}, source_id=preview['id'])
        await manager.execute(second['id'], 123)
        assert manager.latest_rollback_source() is None
    asyncio.run(scenario())


def exclusions_file(tmp_path, barcode):
    target = tmp_path / 'data' / 'exclusions.xlsx'
    target.parent.mkdir(exist_ok=True)
    book = Workbook()
    book.active.append(['ШК', 'ID кабинета 1', 'ID кабинета 2'])
    book.active.append([barcode, 'seller1', None])
    book.save(target)


def test_zero_protects_excluded_alias_and_other_cabinet(tmp_path):
    exclusions_file(tmp_path, 'alias1')
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1', 'cabinet_2'})
        assert '101' not in preview['targets']['cabinet_1:11']
        assert preview['excluded_positions'] == ['cabinet_1:11:101']
        manager.zero_authorized.add((123, preview['id']))
        await manager.execute(preview['id'], 123)
        assert FakeWB.stock['cabinet_1', 11, 101] == 5
        assert FakeWB.stock['cabinet_2', 11, 101] == 0
    asyncio.run(scenario())


def test_distribution_protects_shared_chrt(tmp_path):
    exclusions_file(tmp_path, 'alias1')
    path = tmp_path / 'TT — Main.xlsx'
    make_excel(path, [('sku1', 20), ('sku2', 10)])
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('distribution', 123, {'cabinet_1'}, files=[path])
        assert preview['targets']['cabinet_1:11'] == {'102': 10}
        await manager.execute(preview['id'], 123)
        assert FakeWB.stock['cabinet_1', 11, 101] == 5
    asyncio.run(scenario())


def test_exclusions_changed_after_preview_abort(tmp_path):
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'})
        manager.zero_authorized.add((123, preview['id']))
        exclusions_file(tmp_path, 'sku1')
        with pytest.raises(StockOperationError, match='Исключения изменились'):
            await manager.execute(preview['id'], 123)
        assert not FakeWB.puts
    asyncio.run(scenario())


def test_rollback_respects_new_exclusion(tmp_path):
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'})
        manager.zero_authorized.add((123, preview['id']))
        await manager.execute(preview['id'], 123)
        exclusions_file(tmp_path, 'sku1')
        rollback = await manager.preview('rollback', 123, {'cabinet_1'}, source_id=preview['id'])
        assert rollback['targets']['cabinet_1:11'] == {'102': 9}
        await manager.execute(rollback['id'], 123)
        assert FakeWB.stock['cabinet_1', 11, 101] == 0
    asyncio.run(scenario())


def test_zero_selected_warehouses_only(tmp_path, monkeypatch):
    async def warehouses(client):
        return [Warehouse(client.cabinet.key, client.cabinet.name, warehouse, str(warehouse))
                for warehouse in (11, 12)]
    monkeypatch.setattr(FakeWB, 'get_warehouses', warehouses)
    FakeWB.stock['cabinet_1', 12, 101] = 7
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'}, warehouse_keys={'cabinet_1:12'})
        assert set(preview['targets']) == {'cabinet_1:12'}
        manager.zero_authorized.add((123, preview['id']))
        await manager.execute(preview['id'], 123)
        assert FakeWB.stock['cabinet_1', 11, 101] == 5
        assert FakeWB.stock['cabinet_1', 12, 101] == 0
        with pytest.raises(StockOperationError, match='выбор складов'):
            await manager.preview('zero', 123, {'cabinet_1'}, warehouse_keys={'cabinet_2:11'})
    asyncio.run(scenario())


def test_permissions_on_manager(tmp_path):
    manager = StockManager(settings(tmp_path))
    async def scenario():
        for actor in (0, 456):
            with pytest.raises(StockOperationError, match='администратору'):
                await manager.preview('zero', actor, {'cabinet_1'})
            with pytest.raises(StockOperationError, match='администратору'):
                await manager.execute('123', actor)
        assert not FakeWB.puts
    asyncio.run(scenario())


def test_partial_rollback_accumulates_restored_positions(tmp_path):
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'})
        manager.zero_authorized.add((123, preview['id']))
        await manager.execute(preview['id'], 123)
        FakeWB.stock['cabinet_1', 11, 101] = 2
        rollback = await manager.preview('rollback', 123, {'cabinet_1'}, source_id=preview['id'])
        await manager.execute(rollback['id'], 123)
        FakeWB.stock['cabinet_1', 11, 101] = 0
        next_rollback = await manager.preview('rollback', 123, {'cabinet_1'}, source_id=preview['id'])
        assert next_rollback['targets']['cabinet_1:11'] == {'101': 5}
        assert not next_rollback['conflicts']
        await manager.execute(next_rollback['id'], 123)
        assert manager.latest_rollback_source() is None
    asyncio.run(scenario())


def test_timeout_after_write_is_verified_and_backup_precedes_put(tmp_path, monkeypatch):
    manager = StockManager(settings(tmp_path))
    original = FakeWB.put_fbs_stocks
    async def timeout(client, warehouse, amounts):
        records = manager.list_records()
        assert records[0]['before']['cabinet_1:11'] == {'101': 5, '102': 9}
        await original(client, warehouse, amounts)
        raise TimeoutError('response lost')
    monkeypatch.setattr(FakeWB, 'put_fbs_stocks', timeout)
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'})
        manager.zero_authorized.add((123, preview['id']))
        result = await manager.execute(preview['id'], 123)
        assert result['status'] == 'partial'
        assert result['verified_count'] == 2
        assert len(FakeWB.puts) == 1
        with pytest.raises(StockOperationError):
            await manager.execute(preview['id'], 123)
    asyncio.run(scenario())


def test_success_response_without_actual_write_is_not_success(tmp_path, monkeypatch):
    async def ignored(client, warehouse, amounts):
        return None
    monkeypatch.setattr(FakeWB, 'put_fbs_stocks', ignored)
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'})
        manager.zero_authorized.add((123, preview['id']))
        result = await manager.execute(preview['id'], 123)
        assert result['status'] == 'partial'
        assert result['verified_count'] == 0
        assert manager.latest_rollback_source() is None
    asyncio.run(scenario())


def test_history_shows_ten_but_retains_backups(tmp_path):
    manager = StockManager(settings(tmp_path))
    async def scenario():
        for index in range(12):
            FakeWB.stock['cabinet_1', 11, 101] = index + 1
            preview = await manager.preview('zero', 123, {'cabinet_1'})
            manager.zero_authorized.add((123, preview['id']))
            await manager.execute(preview['id'], 123)
        assert len(manager.list_records()) == 10
        assert len(manager.list_records(None)) == 12
    asyncio.run(scenario())


def test_partial_failed_rollback_can_resume(tmp_path, monkeypatch):
    manager = StockManager(settings(tmp_path))
    original = FakeWB.put_fbs_stocks
    async def fail_second(client, warehouse, amounts):
        if client.cabinet.key == 'cabinet_2':
            raise TimeoutError('unavailable')
        await original(client, warehouse, amounts)
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1', 'cabinet_2'})
        manager.zero_authorized.add((123, preview['id']))
        await manager.execute(preview['id'], 123)
        monkeypatch.setattr(FakeWB, 'put_fbs_stocks', fail_second)
        rollback = await manager.preview('rollback', 123, {'cabinet_1', 'cabinet_2'}, source_id=preview['id'])
        assert (await manager.execute(rollback['id'], 123))['status'] == 'partial'
        monkeypatch.setattr(FakeWB, 'put_fbs_stocks', original)
        retry = await manager.preview('rollback', 123, {'cabinet_1', 'cabinet_2'}, source_id=preview['id'])
        assert 'cabinet_1:11' not in retry['targets']
        assert (await manager.execute(retry['id'], 123))['status'] == 'verified'
        assert manager.latest_rollback_source() is None
    asyncio.run(scenario())


def test_preview_expiry_owner_and_restart(tmp_path):
    from datetime import datetime, timedelta, timezone
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'})
        with pytest.raises(StockOperationError, match='другому'):
            manager.get_preview(preview['id'], 456)
        restarted = StockManager(settings(tmp_path))
        with pytest.raises(StockOperationError, match='отменён'):
            restarted.get_preview(preview['id'], 123)
        preview['created_at'] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        stock_ops._save(manager._preview_path(preview['id']), preview)
        with pytest.raises(StockOperationError, match='15 минут'):
            manager.get_preview(preview['id'], 123)
        assert not FakeWB.puts
    asyncio.run(scenario())


def test_lock_rejects_parallel_write(tmp_path):
    manager = StockManager(settings(tmp_path))
    async def scenario():
        async with manager.lock:
            with pytest.raises(StockOperationError, match='другая операция'):
                await manager.execute('abc', 123)
        assert not FakeWB.puts
    asyncio.run(scenario())


def test_large_operation_is_chunked_and_verified(tmp_path, monkeypatch):
    async def catalog(client):
        variants = {ident: ProductVariant(client.cabinet.key, ident, ident, (str(ident),))
                    for ident in range(1, 503)}
        return {str(ident): variant for ident, variant in variants.items()}, variants
    monkeypatch.setattr(FakeWB, 'get_catalog_variants', catalog)
    for ident in range(1, 503):
        FakeWB.stock['cabinet_1', 11, ident] = 2
    manager = StockManager(settings(tmp_path))
    async def scenario():
        preview = await manager.preview('zero', 123, {'cabinet_1'})
        manager.zero_authorized.add((123, preview['id']))
        result = await manager.execute(preview['id'], 123)
        assert result['verified_count'] == 502
        assert [len(chunk) for cabinet, warehouse, chunk in FakeWB.puts] == [500, 2]
    asyncio.run(scenario())
