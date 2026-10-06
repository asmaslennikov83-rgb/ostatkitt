import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.config as config
from app.config import CabinetConfig, Settings


@pytest.fixture
def bot_module(tmp_path, monkeypatch):
    settings = Settings('fake', {123, 456}, (
        CabinetConfig('cabinet_1', 'TT', 'fake1'),
        CabinetConfig('cabinet_2', 'Group', 'fake2'),
    ), 14, 4, 2, 20, 4, base_dir=tmp_path, admin_ids={123})
    monkeypatch.setattr(config, 'load_settings', lambda: settings)
    sys.modules.pop('app.bot', None)
    module = importlib.import_module('app.bot')
    yield module
    sys.modules.pop('app.bot', None)


def query(data, actor=123):
    return SimpleNamespace(data=data, from_user=SimpleNamespace(id=actor), answer=AsyncMock(),
                           message=SimpleNamespace(answer=AsyncMock(), answer_document=AsyncMock(),
                                                   edit_reply_markup=AsyncMock()))


def test_non_admin_can_download_but_not_upload(bot_module, tmp_path):
    bot_module.pending_runs[456] = {'zip': tmp_path / 'run.zip', 'files': [tmp_path / 'warehouse.xlsx']}
    request = query('dist:both', 456)
    asyncio.run(bot_module.distribution_action(request))
    assert request.message.answer_document.await_count == 2
    assert 'только администратору' in request.message.answer.call_args.args[0]


@pytest.mark.parametrize('action, count', [('download', 2), ('upload', 0), ('both', 2)])
def test_three_distribution_actions(bot_module, tmp_path, action, count):
    bot_module.pending_runs[123] = {'zip': tmp_path / 'run.zip', 'files': [tmp_path / 'warehouse.xlsx']}
    request = query(f'dist:{action}')
    asyncio.run(bot_module.distribution_action(request))
    assert request.message.answer_document.await_count == count
    if action != 'download':
        assert 'кабинеты' in request.message.answer.call_args.args[0]


def test_zero_requires_phrase_before_final_button(bot_module, monkeypatch):
    monkeypatch.setattr(bot_module.stock_manager, 'get_preview', lambda ident, actor: {'id': 'abc', 'kind': 'zero'})
    request = query('stock:confirm:abc')
    asyncio.run(bot_module.stock_confirm(request))
    assert bot_module.user_modes[123] == 'zero_confirm:abc'
    assert not bot_module.stock_manager.zero_authorized
    message = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock())
    asyncio.run(bot_module.zero_phrase(message))
    assert (123, 'abc') in bot_module.stock_manager.zero_authorized
    keyboard = message.answer.call_args.kwargs['reply_markup']
    assert keyboard.inline_keyboard[0][0].callback_data == 'stock:execute:abc'


def test_warehouse_toggle_and_stale_selection(bot_module):
    selection = {'id': 'fresh', 'choices': {'cabinet_1:11': 'TT — Main', 'cabinet_2:11': 'Group — Main'},
                 'selected': set()}
    bot_module.zero_selections[123] = selection
    asyncio.run(bot_module.toggle_zero_warehouse(query('stock:warehouse:old:1')))
    assert not selection['selected']
    asyncio.run(bot_module.toggle_zero_warehouse(query('stock:warehouse:fresh:1')))
    assert selection['selected'] == {'cabinet_2:11'}
    asyncio.run(bot_module.toggle_zero_warehouse(query('stock:warehouse:fresh:1')))
    assert not selection['selected']


def test_non_admin_cannot_select_zero_warehouses(bot_module, monkeypatch):
    choices = AsyncMock()
    monkeypatch.setattr(bot_module.stock_manager, 'warehouse_choices', choices)
    request = query('stock:warehouses', 456)
    asyncio.run(bot_module.choose_zero_warehouses(request))
    choices.assert_not_awaited()
    assert request.answer.call_args.kwargs['show_alert']
