#!/usr/bin/env python3
"""Mock kdf daemon for UI iteration without a chain.

Stdlib only. Mirrors the envelopes pinned against the LIVE regtest daemon in
F1/F2 (probes + lp_ordermatch.rs source reads; doc/elektron.md sections 9-11):

- legacy top-level: {"userpass", "method", ...} -> {"result": ...} (or the
  legacy {"error": "<string>"} form)
- v2: {"mmrpc": "2.0", "method", "params"} -> {"mmrpc": "2.0", "result": ...}
  or {"mmrpc": "2.0", "error": {error_path, error_type, error_trace, error_data}}
- success wrap is MIXED on real daemons (pinned live, regtest 3.0.0-beta):
  my_balance and orderbook answer BARE, version / get_enabled_coins /
  my_orders wrap in "result"
- orderbook entries are the legacy AggregatedOrderbookEntry: prices and
  volumes as decimal strings plus *_fraction/{numer,denom} and *_rat pairs,
  is_mine, flattened confs (base_confs/base_nota/rel_confs/rel_nota),
  numasks/numbids (no underscore), totals as strings
- setprice = MAKER (lp_ordermatch.rs create_maker_order) -> BARE MakerOrder
  result (no "Created" wrapper, uuid at top level; pinned on regtest
  2026-10-02), its order enters the book; sell/buy = TAKER (lp_auto_buy ->
  TakerOrderBuilder) -> SellBuyResponse, NEVER touches the book
- my_orders -> {"maker_orders": {uuid: {...}}, "taker_orders": {uuid: {...}}};
  the taker value is TakerOrderForRpc: amounts/order metadata live in
  o.request (the SellBuyResponse), only created_at/order_type/cancellable
  sit at top level (pinned live)
- cancel_order -> {"result": "success"};
  cancel_all_orders -> {"result": {"cancelled": [uuids], "currently_matching": []}}
- stream::orderbook::enable -> {"streamer_id": "ORDERBOOK_UPDATE:orbk/ELEK:tBTC"}
  (slash form + trailing data frames, as observed live)
- SSE frames carry NO initial snapshot (the real streamer has none); live
  order_data is the rat-serialized OrderbookP2PItem ([[numer...], [denom...]]
  pairs, created_at in SECONDS, no is_mine) — the UI treats every event as a
  dirty flag and re-polls, so exact rat values are cosmetic here.
- RemovedItem's order_data is the bare uuid.
- coins are STATE-driven: legacy `electrum` activates (adding a fake entry +
  canned balance), legacy `disable_coin` removes; get_enabled_coins is the
  read side. The FIRST electrum call per ticker answers an empty 500 once —
  the live first-call shape the real UI must tolerate and retry past.

Known deviations from the real daemon (kept provisional, marked in code):
- maker_orders value shape (MakerOrderForMyOrdersRpc) is a plausible
  reconstruction (unpinned surface; the UI reads it defensively)
- all books are for the fixed pair ELEK/tBTC; other pairs answer an empty book
- GET /fx/rates.json serves the canned electrs rich-shape rate (the real rate
  file is written by electrs' fetcher and served by elek-web, §13)

Modes (POST {"method": "mock.set_mode", "params": {"mode": ...}} or --mode):
  ok     - canned success (default); SSE carries the scripted add/remove cycle
  static - like ok, but the cycle ticker is paused (stable book for
           deterministic headless DOM dumps; RPC/SSE still work, SSE just
           stays quiet)
  err    - every RPC method answers the v2 error form
  empty  - every RPC method answers HTTP 500 with an EMPTY body
  slow   - every RPC method answers after 2s

Usage:  python3 web/test/mock_daemon.py [--port 7993] [--mode ok]
CORS is wide open (*) so the SPA on http://localhost:3000 can talk to it.
"""

import argparse
import json
import re
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MODE_LOCK = threading.Lock()
MODE = {'value': 'ok'}

BOOK_LOCK = threading.Lock()
BOOK_PAIR = ('ELEK', 'tBTC')   # already alphabetically sorted (orbk topic form)
STREAMER_ID = 'ORDERBOOK_UPDATE:orbk/%s:%s' % BOOK_PAIR
MY_PUBKEY = 'aa11' * 16
OTHER_PUBKEY = 'bb22' * 16
MY_ADDRESS = 'be1qmockmyaddress0000000000000000000000000000'

# uuid -> (side, price [tBTC per ELEK], base_vol [ELEK], is_mine)
BASE_BOOK = {
    '11e5a1f0-0000-4100-8000-000000000001': ('ask', '0.00002', '400', False),
    '11e5a1f0-0000-4100-8000-000000000002': ('ask', '0.0000215', '60', True),   # own order
    '11e5a1f0-0000-4100-8000-000000000003': ('bid', '0.0000098', '1200', False),
    '11e5a1f0-0000-4100-8000-000000000004': ('bid', '0.0000089', '77', False),
}

CONF_SETTINGS = {'base_confs': 2, 'base_nota': False, 'rel_confs': 1, 'rel_nota': False}


def _rat(x):
    """Decimal string -> [[numer-limbs], [denom-limbs]], the rat wire form."""
    x = str(Decimal(str(x)).normalize())
    if 'e' in x or 'E' in x:
        x = format(Decimal(str(x)), 'f')
    if '.' in x:
        intpart, frac = x.split('.')
    else:
        intpart, frac = x, ''
    numer = int((intpart + frac).lstrip('0') or '0')
    denom = 10 ** len(frac)
    if numer == 0:
        numer = 0 if denom == 1 else numer
    sign = 1 if x.startswith('-') else 0
    body = [abs(numer)] if numer else [0]
    return [[sign, body], [1, [denom]]]


def _entry(uuid, side, price, base_vol, mine):
    """Legacy orderbook entry (one orientation: the BOOK_PAIR book)."""
    rel_vol = str(Decimal(base_vol) * Decimal(price))
    created_at = int(time.time()) - 42
    e = {
        'address': MY_ADDRESS if mine else 'be1qmockotheraddress0000000000000000000',
        'age': 42,
        'base_confs': CONF_SETTINGS['base_confs'],
        'base_max_volume': base_vol,
        'base_max_volume_fraction': {'numer': base_vol, 'denom': '1'},
        'base_max_volume_rat': _rat(base_vol),
        'base_max_volume_aggr': base_vol,
        'base_max_volume_aggr_fraction': {'numer': base_vol, 'denom': '1'},
        'base_max_volume_aggr_rat': _rat(base_vol),
        'base_min_volume': str(Decimal(base_vol) / Decimal(10)),
        'base_min_volume_fraction': {'numer': '1', 'denom': None},
        'base_nota': CONF_SETTINGS['base_nota'],
        'coin': BOOK_PAIR[0],           # the underlying order's base
        'is_mine': mine,
        'maxvolume': base_vol,          # key spelled "maxvolume" (legacy RPC)
        'max_volume_fraction': {'numer': base_vol, 'denom': '1'},
        'max_volume_rat': _rat(base_vol),
        'min_volume': str(Decimal(base_vol) / Decimal(10)),
        'min_volume_fraction': {'numer': '1', 'denom': None},
        'min_volume_rat': _rat(base_vol),
        'price': price,
        'price_fraction': {'numer': price, 'denom': '1'},
        'price_rat': _rat(price),
        'pubkey': MY_PUBKEY if mine else OTHER_PUBKEY,
        'rel_confs': CONF_SETTINGS['rel_confs'],
        'rel_max_volume': rel_vol,
        'rel_max_volume_fraction': {'numer': rel_vol, 'denom': '1'},
        'rel_max_volume_rat': _rat(rel_vol),
        'rel_max_volume_aggr': rel_vol,
        'rel_max_volume_aggr_fraction': {'numer': rel_vol, 'denom': '1'},
        'rel_max_volume_aggr_rat': _rat(rel_vol),
        'rel_min_volume': str(Decimal(base_vol) * Decimal(price) / Decimal(10)),
        'rel_min_volume_fraction': {'numer': '1', 'denom': None},
        'rel_nota': CONF_SETTINGS['rel_nota'],
        'uuid': uuid,
    }
    # keep the fake fractions dimensionally sane (daemon emits exact numer/denom)
    e['base_min_volume_fraction'] = {'numer': base_vol, 'denom': str(10)}
    e['min_volume_fraction'] = {'numer': base_vol, 'denom': str(10)}
    e['rel_min_volume_fraction'] = {'numer': rel_vol, 'denom': str(10)}
    e['price_fraction'] = {'numer': price, 'denom': '1'}
    return e


def _maker_value(uuid, side, price, base_vol, mine, created_at_ms):
    return {
        'available_amount': base_vol,
        'base': BOOK_PAIR[0],
        'base_orderbook_ticker': BOOK_PAIR[0],
        'cancellable': True,
        'conf_settings': dict(CONF_SETTINGS),
        'created_at': created_at_ms,
        'matches': {},
        'max_base_vol': base_vol,
        'min_base_vol': str(Decimal(base_vol) / Decimal(10)),
        'price': price,
        'rel_orderbook_ticker': BOOK_PAIR[1],
        'uuid': uuid,
        **({'action': 'Sell'} if side == 'ask' else {}),
    }


def _taker_value(uuid, side, price, base_vol, created_at_ms):
    """PROVISIONAL: taker_orders value fields are not pinned yet."""
    return {
        'action': 'Sell' if side == 'sell' else 'Buy',
        'base': BOOK_PAIR[0],
        'rel': BOOK_PAIR[1],
        'uuid': uuid,
        'base_amount': base_vol,
        'rel_amount': str(Decimal(base_vol) * Decimal(price) * (Decimal(1) if side == 'sell' else Decimal(1))),
        'price': price,
        'created_at': created_at_ms,
        'cancellable': True,
        'matches': {},
        'method': 'request',
        'sender_pubkey': MY_PUBKEY,
        'order_type': {'type': 'GoodTillCancelled'},
    }


def _mode():
    with MODE_LOCK:
        return MODE['value']


def _set_mode(value):
    with MODE_LOCK:
        MODE['value'] = value
    return value


def _v2_error(error_type, error_path='dispatcher', detail=None):
    err = {'error_path': error_path, 'error_type': error_type,
           'error_trace': 'mock_daemon:1]'}
    if detail is not None:
        err['error_data'] = detail
    return {'mmrpc': '2.0', 'error': err, 'error_data': detail}


def _is_v2(req):
    return isinstance(req, dict) and req.get('mmrpc') == '2.0'


def _respond(req, result):
    # a v2 request gets the v2 success form, a legacy request the legacy form
    if _is_v2(req):
        return {'mmrpc': '2.0', 'result': result}
    return {'result': result}


def _empty_legacy_error(req):
    """legacy {"error": "<string>"} form"""
    return {'error': 'mock: not found'}


def _sorted_book():
    """(asks, bids) sorted the way the real orderbook RPC sorts them."""
    with BOOK_LOCK:
        items = list(STATE['book'].values())
    asks = sorted((i for i in items if i['side'] == 'ask'),
                  key=lambda i: float(i['price']))
    bids = sorted((i for i in items if i['side'] == 'bid'),
                  key=lambda i: -float(i['price']))
    return asks, bids


def _pair_or_none(params):
    base = params.get('base')
    rel = params.get('rel')
    return base, rel


def _empty_book_response(base, rel):
    return {
        'base': base, 'rel': rel,
        'netid': 0, 'timestamp': int(time.time()),
        'askdepth': [], 'asks': [],
        'biddepth': [], 'bids': [],
        'numasks': 0, 'numbids': 0,
        'total_asks_base_vol': '0',
        'total_asks_base_vol_fraction': {'numer': '0', 'denom': '1'},
        'total_bids_base_vol': '0',
        'total_bids_base_vol_fraction': {'numer': '0', 'denom': '1'},
        'total_bids_rel_vol': '0',
        'total_bids_rel_vol_fraction': {'numer': '0', 'denom': '1'},
    }


def _book_response(base, rel):
    if (base, rel) != BOOK_PAIR:
        # the real daemon serves books per pair; unknown pairs are simply empty
        return _empty_book_response(base, rel)
    asks, bids = _sorted_book()
    totals_base_asks = sum(Decimal(i['base_vol']) for i in asks)
    totals_base_bids = sum(Decimal(i['base_vol']) for i in bids)
    totals_rel_bids = sum(Decimal(i['base_vol']) * Decimal(i['price']) for i in bids)
    return {
        'base': BOOK_PAIR[0], 'rel': BOOK_PAIR[1],
        'netid': 0, 'timestamp': int(time.time()),
        'askdepth': [],
        'asks': [_entry(u, i['side'], i['price'], i['base_vol'], i['mine']) for u, i in [(i['uuid'], i) for i in asks]],
        'biddepth': [],
        'bids': [_entry(u, i['side'], i['price'], i['base_vol'], i['mine']) for u, i in [(i['uuid'], i) for i in bids]],
        'numasks': len(asks),       # no underscore (pinned live, F2)
        'numbids': len(bids),
        'total_asks_base_vol': str(totals_base_asks),
        'total_asks_base_vol_fraction': {'numer': str(totals_base_asks), 'denom': '1'},
        'total_asks_base_vol_rat': _rat(totals_base_asks),
        'total_bids_base_vol': str(totals_base_bids),
        'total_bids_base_vol_fraction': {'numer': str(totals_base_bids), 'denom': '1'},
        'total_bids_base_vol_rat': _rat(totals_base_bids),
        'total_bids_rel_vol': str(totals_rel_bids),
        'total_bids_rel_vol_fraction': {'numer': str(totals_rel_bids), 'denom': '1'},
        'total_bids_rel_vol_rat': _rat(totals_rel_bids),
    }


STATE = {
    # uuid -> {'uuid','side','price','base_vol','mine','created_at'}  (makers)
    'book': {},
    # uuid -> taker order value (sell/buy rest here — never in the book)
    'takers': {},
    # uuid -> {'swap_type': MakerV1|..., 'swap_data': <per-type swap struct>}
    'active': {},
    # history: newest first, same tagged form as the v2 API returns
    'hist': [],
    # ticker -> enabled-coins entry (electrum/disable_coin mutate this;
    # get_enabled_coins reads it). The live answer carries ONLY ticker+address
    # (pinned regtest 2026-10-02) — no coin/rpcport siblings.
    'coins': {
        'ELEK': {'ticker': 'ELEK', 'address': MY_ADDRESS},
        'tBTC': {'ticker': 'tBTC', 'address': 'tb1qmocktbtcaddress00000000000000000000'},
    },
    # ticker -> {'balance','unspendable_balance'} for the BARE my_balance;
    # coins activated via electrum join with a canned balance below.
    'balances': {
        'ELEK': {'balance': '231.4567', 'unspendable_balance': '0'},
        'tBTC': {'balance': '0.5', 'unspendable_balance': '0'},
    },
    # electrum first-call-500 bookkeeping (one empty 500 per ticker, like a
    # cold daemon sometimes answers on the first activation call)
    'electrum_seen': set(),
}


def _record(uuid, side, price, base_vol, mine):
    return {'uuid': uuid, 'side': side, 'price': price, 'base_vol': base_vol,
            'mine': mine, 'created_at': int(time.time() * 1000) - 123456789}


ORIG = {}   # pristine book records (the cycle re-adds from here)
for _u, (_side, _price, _vol, _mine) in BASE_BOOK.items():
    ORIG[_u] = _record(_u, _side, _price, _vol, _mine)
    STATE['book'][_u] = dict(ORIG[_u])   # full initial book incl. the own ask


# ---- swap surfaces (F3) ------------------------------------------------------
# Shapes pinned 2026-10-02 (lp_swap.rs + swap_v2_rpcs.rs + streamer_ids.rs):
# - v2 active_swaps {include_status} -> wrapped {uuids, statuses:{}|{uuid:tag}};
#   the legacy twin answers BARE {uuids, statuses: null|{v1 items}} (statuses
#   is an Option in the legacy response — null without include_status, and
#   include_status=true fills V1 swaps only).
# - v2 my_recent_swaps -> wrapped {swaps: [tagged items], from_uuid, skipped,
#   limit, total, page_number, total_pages, found_records}; items are
#   SwapRpcData = {"swap_type": MakerV1|TakerV1|MakerV2|TakerV2, "swap_data":
#   <per-type>}. Uniform across swap versions — that is why the v2 route (not
#   the legacy one, whose item shape differs per version) feeds history.
# - v2 my_swap_status {uuid} -> one tagged item, or v2 error NoSwapWithUuid.
#   V1 payloads carry the swap secret inside the Started event (no
#   hide_secrets on this route) — consumers must render curated fields only.
# - legacy my_swap_status reads params.uuid on the LEGACY envelope too
#   (lp_swap.rs:1111) and answers MySwapStatusResponse: flatten SavedSwap
#   plus "type": Maker|Taker, my_info, recoverable, is_finished and
#   is_success (absent when unfinished); hide_secrets applied.
# Event wrapper timestamps are milliseconds (live pin: 13 digits) while
# started_at inside Started data is seconds.

SWAP_TS = int(time.time())

# a canned chain, kept short but with realistic V1 event names
_TAKER_OK_CHAIN = ['Negotiated', 'TakerFeeSent', 'MakerPaymentReceived',
                   'TakerPaymentSent', 'TakerPaymentSpent', 'Finished']
_MAKER_OK_CHAIN = ['Negotiated', 'MakerPaymentSent', 'TakerPaymentSpent', 'Finished']
_TAKER_FAIL_CHAIN = ['Negotiated', 'TakerFeeSent', 'Error', 'Finished']


def _kdata(kind):
    """Curated-safe data for canned events (a hash only, no secrets needed)."""
    if kind == 'Negotiated':
        return {'taker_payment_locktime': SWAP_TS + 7200, 'maker_payment_lock': SWAP_TS + 7800}
    if kind in ('TakerFeeSent', 'TakerPaymentSent', 'TakerPaymentSpent',
                'MakerPaymentSent', 'MakerPaymentSpent', 'MakerPaymentSpendConfirmed'):
        return {'tx_hash': '%032x' % (abs(hash(kind)) % 2**128)}
    if kind == 'MakerPaymentReceived':
        return {'tx_hash': 'bb' * 32}
    if kind == 'Error':
        return {'error': 'TakerPaymentValidate failed: mock canned failure'}
    return {}


def _ev(started_ms, seq, kind):
    return {'timestamp': int(started_ms + seq * 30000),
            'event': {'type': kind, 'data': _kdata(kind)}}


def _t_start_data(maker, taker, m_amt, t_amt, started_at):
    """Started event data, TakerSwapData lookalike (live pin, F2). The secret
    field is deliberately present with a fake hex — real V1 payloads carry it
    on the v2 routes, and the UI timeline must never render it."""
    return {
        'taker_coin': taker, 'maker_coin': maker,
        'taker': OTHER_PUBKEY, 'maker': MY_PUBKEY,
        'secret': '6f' * 32,
        'secret_hash': 'e2c0b09eee6324b549184daefb00ef9a9b6f2e7c',
        'lock_duration': 7800,
        'maker_amount': str(m_amt), 'taker_amount': str(t_amt),
        'maker_payment_lock': started_at + 7800,
        'started_at': started_at,
        'maker_payment_confirmations': 2, 'taker_payment_confirmations': 1,
        'maker_payment_requires_nota': False, 'taker_payment_requires_nota': False,
        'maker_coin_start_block': 25062, 'taker_coin_start_block': 24738,
        'maker_payment_trade_fee': {'coin': maker, 'amount': '0.00001',
                                    'paid_from_trading_vol': False},
        'taker_payment_spend_trade_fee': {'coin': taker, 'amount': '0.00002',
                                          'paid_from_trading_vol': True},
    }


def _v1swap(uuid, side, maker, taker, m_amt, t_amt, started_at, kinds, ok=True):
    """TakerSavedSwap/MakerSavedSwap lookalike (one struct for both sides —
    field names coincide; coins/amounts stay None like the real Opt fields).
    `kinds` is the event chain AFTER Started — it drives the UI timeline."""
    data = _t_start_data(maker, taker, m_amt, t_amt, started_at)
    data['uuid'] = uuid
    t = started_at * 1000
    events = [dict(_ev(t, 0, 'Started'),
                   **{'event': {'type': 'Started', 'data': data}})]
    for i, kind in enumerate(kinds, 1):
        events.append(_ev(t, i, kind))
    return {
        'uuid': uuid,
        'my_order_uuid': 'ae2bdaf1-040d-49b6-be7a-565f511c0171',
        'events': events,
        'maker_amount': None, 'maker_coin': None,
        'taker_amount': None, 'taker_coin': None,
        'gui': None, 'mm_version': '3.0.0-beta_mock',
        'success_events': ['Finished'] if ok else [],
        'error_events': [] if ok else ['Error'],
    }


def _my_swap_for_rpc_v2(uuid, my_coin, other_coin, started_at, my_vol, other_vol):
    """MySwapForRpc (V2 swap): my_/other_ perspective, is_finished at top."""
    return {
        'my_coin': my_coin, 'other_coin': other_coin, 'uuid': uuid,
        'started_at': started_at, 'is_finished': True,
        'events': [{'timestamp': started_at * 1000, 'event': {'type': 'Started', 'data': {}}},
                   {'timestamp': started_at * 1000 + 60000, 'event': {'type': 'Finished', 'data': {}}}],
        'maker_volume': other_vol, 'taker_volume': my_vol,
        'premium': '0', 'dex_fee': '0', 'lock_duration': 7800,
        'maker_coin_confs': 2, 'maker_coin_nota': False,
        'taker_coin_confs': 1, 'taker_coin_nota': False, 'swap_version': 2,
    }


def _init_swaps():
    now = int(time.time())
    act = 'c401d0a0-0000-4100-8000-00000000f004'
    act_start_data = _t_start_data('tBTC', 'ELEK', '0', '0', now - 300)
    act_start_data.update({'uuid': act, 'maker_amount': '1', 'taker_amount': '0.5'})
    active = {'swap_type': 'TakerV1', 'swap_data': {
        'uuid': act, 'my_order_uuid': 'be2bdaf1-040d-49b6-be7a-565f511c0172',
        'events': [dict(_ev((now - 300) * 1000, 0, 'Started'),
                        **{'event': {'type': 'Started', 'data': act_start_data}}),
                    _ev((now - 300) * 1000, 1, 'Negotiated'),
                    _ev((now - 300) * 1000, 2, 'TakerFeeSent')],
        'maker_amount': None, 'maker_coin': None,
        'taker_amount': None, 'taker_coin': None,
        'gui': None, 'mm_version': '3.0.0-beta_mock',
        'success_events': [], 'error_events': [],
    }}
    hist = [
        # oldest first is wrong for the API (newest first) — the V2 item gets
        # the newest started_at so list order below is already DESC
        {'swap_type': 'MakerV2', 'swap_data': _my_swap_for_rpc_v2(
            'c401d0a0-0000-4100-8000-00000000f005', 'ELEK', 'tBTC', now - 60,
            '0.5', '0.25')},
        {'swap_type': 'TakerV1', 'swap_data': _v1swap(
            'c401d0a0-0000-4100-8000-00000000f002', 'Taker', 'tBTC', 'ELEK',
            '2', '1', now - 1800, _TAKER_OK_CHAIN, True)},
        {'swap_type': 'MakerV1', 'swap_data': _v1swap(
            'c401d0a0-0000-4100-8000-00000000f003', 'Maker', 'ELEK', 'tBTC',
            '10', '0.005', now - 3600, _MAKER_OK_CHAIN, True)},
        {'swap_type': 'TakerV1', 'swap_data': _v1swap(
            'c401d0a0-0000-4100-8000-00000000f006', 'Taker', 'ELEK', 'tBTC',
            '0', '0', now - 7200, _TAKER_FAIL_CHAIN, False)},
    ]
    with BOOK_LOCK:
        STATE['active'] = {act: active}
        STATE['hist'] = hist


def _legacy_item(swap_type, v1s):
    """MySwapStatusResponse form (legacy routes): side tag + my_info +
    is_finished/is_success, with hide_secrets applied (secret zeroed)."""
    side = 'Taker' if swap_type.startswith('Taker') else 'Maker'
    item = {'type': side}
    s = json.loads(json.dumps(v1s))     # deep copy — STATE is shared
    for ev in s.get('events') or []:
        data = ev.get('event', {}).get('data')
        if isinstance(data, dict) and 'secret' in data:
            data['secret'] = '00' * 32
    item.update(s)
    first = (s.get('events') or [{}])[0].get('event', {}).get('data') or {}
    if first.get('started_at') is not None:
        if side == 'Taker':
            item['my_info'] = {'my_coin': first.get('taker_coin'),
                               'other_coin': first.get('maker_coin'),
                               'my_amount': first.get('taker_amount'),
                               'other_amount': first.get('maker_amount'),
                               'started_at': first['started_at']}
        else:
            item['my_info'] = {'my_coin': first.get('maker_coin'),
                               'other_coin': first.get('taker_coin'),
                               'my_amount': first.get('maker_amount'),
                               'other_amount': first.get('taker_amount'),
                               'started_at': first['started_at']}
    else:
        item['my_info'] = None
    item['recoverable'] = False
    last = (s.get('events') or [{}])[-1].get('event', {}).get('type')
    item['is_finished'] = last == 'Finished'
    if item['is_finished']:
        item['is_success'] = not s.get('error_events')
    return item


def _tagged(uuid):
    with BOOK_LOCK:
        s = STATE['active'].get(uuid) or next(
            (i for i in STATE['hist'] if i['swap_data'].get('uuid') == uuid), None)
    return dict(s) if s else None


_init_swaps()   # canned swap surfaces ready before the first request arrives


def _mode_guard(req):
    """Dispatch table helper: honors mock modes; returns a response or None."""
    method = req.get('method')
    mode = _mode()
    if mode == 'err':
        return _v2_error('MockError', 'mock', 'mode=err: every method fails')
    if mode == 'empty':
        return ('__EMPTY_500__', None)   # sentinel handled by the caller
    if mode == 'slow':
        time.sleep(2)
    return None


def _canned(req):
    """Dispatch table. Returns a serializable response body, or the
    ('__EMPTY_500__', None) sentinel for the empty mode."""
    method = req.get('method')
    params = req.get('params') if _is_v2(req) else req
    if not isinstance(params, dict):
        params = {}

    if method == 'mock.set_mode':
        # control method: accept params in BOTH envelopes (deliberate
        # deviation from the kdf contract — this is mock-only tooling)
        opts = req.get('params') if isinstance(req.get('params'), dict) else {}
        return _respond(req, _set_mode(opts.get('mode', 'ok')))

    if method == 'mock.swap_reset':
        # control method (see mock.set_mode note): rebuild the canned swaps
        _init_swaps()
        with BOOK_LOCK:
            n_act, n_hist = len(STATE['active']), len(STATE['hist'])
        return _respond(req, {'active': n_act, 'hist': n_hist})

    if method == 'mock.swap_event':
        # control method (see mock.set_mode note): append one event to an
        # active swap — {uuid?, type, data?}; without an active one a fresh
        # TakerV1 swap is created. Emitting follows the real SSE frame
        # (SWAP_STATUS message: swap_type + swap_data {uuid, event}).
        opts = req.get('params') if isinstance(req.get('params'), dict) else req
        kind = opts.get('type') or 'Negotiated'
        data = opts.get('data') if opts.get('data') is not None else _kdata(kind)
        now_ms = int(time.time() * 1000)
        with BOOK_LOCK:
            entry = (STATE['active'].get(opts.get('uuid'))
                     or next(iter(STATE['active'].values()), None))
            if entry is None:
                uuid = 'c401d0a0-0000-4100-8000-00000000f020'
                started_at = int(time.time())
                d = _t_start_data('tBTC', 'ELEK', '0.02', '0.01', started_at)
                d['uuid'] = uuid
                sw = {
                    'uuid': uuid, 'my_order_uuid': None,
                    'events': [{'timestamp': started_at * 1000,
                                'event': {'type': 'Started', 'data': d}}],
                    'maker_amount': None, 'maker_coin': None,
                    'taker_amount': None, 'taker_coin': None,
                    'gui': None, 'mm_version': '3.0.0-beta_mock',
                    'success_events': [], 'error_events': [],
                }
                entry = {'swap_type': 'TakerV1', 'swap_data': sw}
                STATE['active'][uuid] = entry
            else:
                uuid = entry['swap_data']['uuid']
            sw = entry['swap_data']
            sw['events'].append({'timestamp': now_ms,
                                 'event': {'type': kind, 'data': data}})
            if kind in ('Finished', 'Terminated'):
                STATE['active'].pop(uuid, None)
                STATE['hist'].insert(0, entry)
            swap_type = entry['swap_type']
        _broadcast_sse(json.dumps({
            '_type': 'SWAP_STATUS',
            'message': {'swap_type': swap_type,
                        'swap_data': {'uuid': uuid,
                                      'event': {'timestamp': now_ms,
                                                'event': {'type': kind,
                                                          'data': data}}}}}))
        return _respond(req, {'uuid': uuid, 'event': kind,
                              'moved_to_history': kind in ('Finished', 'Terminated')})

    guard = _mode_guard(req)
    if guard is not None:
        return guard

    if method == 'version':
        return _respond(req, '3.0.0-beta_mock')

    if method == 'get_enabled_coins':
        with BOOK_LOCK:
            return _respond(req, list(STATE['coins'].values()))

    if method == 'electrum':
        # legacy envelope ONLY (doc/elektron.md section 10: no v2 electrum on
        # this build; top-level coin/servers/required_confirmations). Faithful
        # cold-daemon behavior: the FIRST activation call for a ticker answers
        # the real empty-500 shape once — get_enabled_coins is the truth after
        # that. The success body is placeholder-ish (provisional; the real
        # success form is unpinned and the UI never reads it — it polls).
        coin = params.get('coin') or req.get('coin') or ''
        confs = (params.get('required_confirmations')
                 if params.get('required_confirmations') is not None
                 else req.get('required_confirmations'))
        # real tickers are case-sensitive and lowercase-prefixed on the test
        # chains (rELEK, rBTC, tBTC) — uppercase-only would reject them (live
        # evidence: the first harness run never activated rELEK)
        if not re.match(r'^[A-Za-z0-9]{2,20}$', str(coin)):
            # live-pinned (regtest 2026-10-02): an unknown ticker is NOT
            # named — the daemon answers the missing-param legacy string
            return {'error': 'rpc:198] RPC call failed: legacy:144] '
                             'lp_coins:6249] mm2 param is not set neither in '
                             'coins config nor enable request, assuming that '
                             'coin is not supported'}
        with BOOK_LOCK:
            first = str(coin) not in STATE['electrum_seen']
            STATE['electrum_seen'].add(str(coin))
            if not STATE['coins'].get(coin):
                # same shape as the pinned live answer: ticker+address ONLY
                STATE['coins'][coin] = {
                    'ticker': coin,
                    'address': 'be1qelec%s000000000000' % str(coin).lower()[:12],
                }
                STATE['balances'].setdefault(
                    coin, {'balance': '0.5', 'unspendable_balance': '0'})
        if first:
            return ('__EMPTY_500__', None)   # real first-call shape (live evidence)
        # PINNED live (regtest 2026-10-02): the activation answer is BARE —
        # result:"success" as a FIELD plus the coin state as siblings
        return {
            'result': 'success', 'address': STATE['coins'][coin]['address'],
            'balance': str(Decimal(STATE['balances'].get(coin, {'balance': '0'})['balance'])),
            'unspendable_balance': '0', 'coin': coin,
            'required_confirmations': confs or 2,
            'requires_notarization': False, 'mature_confirmations': 1,
        }

    if method == 'disable_coin':
        # legacy top-level coin; an unknown ticker must error. PINNED live
        # (regtest 2026-10-02): the success form is wrapped
        # {"result": {coin, cancelled_orders, passivized}} and answers after
        # the coin stopped (~100ms); the real one also CANCELS the coin's
        # maker orders — the canned book is not tied to activation here.
        coin = params.get('coin') or req.get('coin') or ''
        with BOOK_LOCK:
            if coin not in STATE['coins']:
                # live-pinned (regtest 2026-10-02): the error string inlines the
                # ENUM name, not the requested ticker, plus status siblings
                return {'error': 'No such coin: NoSuchCoin!!',
                        'orders': {'matching': [], 'cancelled': []},
                        'active_swaps': []}
            STATE['coins'].pop(coin, None)
        return _respond(req, {'coin': coin, 'cancelled_orders': [], 'passivized': False})

    if method == 'trade_preimage':
        # v2 shape pinned live on regtest (F1/F2, 2026-10-02): base_coin_fee/
        # rel_coin_fee + total_fees rows, each with required_balance.
        vol = params.get('volume') or '1'
        fees = [
            {'coin': 'ELEK', 'amount': '0.0001', 'type': 'Maker/TradeFee'},
            {'coin': 'tBTC', 'amount': '0.00001', 'type': 'Taker/TradeFee'},
        ]
        return _respond(req, {
            'base': params.get('base'), 'rel': params.get('rel'),
            'swap_method': params.get('swap_method'),
            'volume': vol,
            'required_balance': {'coin': params.get('base') or 'ELEK', 'amount': vol},
            'total_fees': fees,
        })

    if method == 'my_balance':
        # BARE on the real daemon (no "result" wrapper) — do not wrap;
        # an inactive coin errors (activation-first contract)
        coin = params.get('coin') or req.get('coin')
        with BOOK_LOCK:
            st = STATE['coins'].get(coin)
            if st is None:
                return {'error': 'my_balance: %s is not enabled' % coin}
            b = STATE['balances'].get(coin, {'balance': '0', 'unspendable_balance': '0'})
        return {'coin': coin, 'balance': b['balance'],
                'unspendable_balance': b['unspendable_balance'], 'address': st['address']}

    if method == 'orderbook':
        # BARE on the real daemon (no "result" wrapper) — do not wrap
        return _book_response(params.get('base', 'ELEK'), params.get('rel', 'tBTC'))

    if method in ('sell', 'buy'):
        if _is_v2(req):
            # faithful: this build has NO v2 sell/buy
            return _v2_error('NoSuchMethod', 'dispatcher', {'data': [method]})
        # TAKER: SellBuyResponse, and the book does NOT change (lp_auto_buy)
        uuid = '11e5a1f0-0000-4100-8000-%012d' % (len(STATE['takers']) + 1)
        with BOOK_LOCK:
            STATE['takers'][uuid] = _taker_value(uuid, method, params.get('price', '0.00001'),
                                                 params.get('volume', '1'), int(time.time() * 1000))
        return _respond(req, {
            'action': 'Sell' if method == 'sell' else 'Buy',
            'base': params.get('base', BOOK_PAIR[0]),
            'rel': params.get('rel', BOOK_PAIR[1]),
            'method': 'request',
            'sender_pubkey': MY_PUBKEY,
            'dest_pub_key': OTHER_PUBKEY,
            'uuid': uuid,
            'match_by': {'type': 'Any'},
            'conf_settings': dict(CONF_SETTINGS),
            'order_type': params.get('order_type', {'type': 'GoodTillCancelled'}),
            'min_volume': '0.0001',
        })

    if method == 'setprice':
        if _is_v2(req):
            return _v2_error('NoSuchMethod', 'dispatcher', {'data': [method]})
        # MAKER — pinned live (regtest 2026-10-02): the result is a BARE
        # MakerOrder (no 'Created' wrapper, uuid top level); the order joins
        # the book via the P2P loopback, SSE listeners get their own frame.
        uuid = '11e5a1f0-0000-4100-8000-%012d' % (len(STATE['book']) + 1)
        price = params.get('price', '0.00001')
        base_vol = params.get('volume', '10')
        created_at_ms = int(time.time() * 1000)
        with BOOK_LOCK:
            STATE['book'][uuid] = {
                'uuid': uuid, 'side': 'ask', 'price': price, 'base_vol': base_vol,
                'mine': True, 'created_at': created_at_ms,
            }
        # PINNED live (regtest 2026-10-02): the setprice result is a BARE
        # MakerOrder — no 'Created' wrapper, uuid at top level.
        rel_dust = Decimal('0.0001')
        min_base_vol = rel_dust / Decimal(str(price))
        order_result = {
            'base': BOOK_PAIR[0],
            'rel': BOOK_PAIR[1],
            'price': str(price),
            'price_rat': _rat(price),
            'max_base_vol': str(base_vol),
            'max_base_vol_rat': _rat(base_vol),
            'min_base_vol': format(min_base_vol, 'f'),
            'min_base_vol_rat': _rat(min_base_vol),
            'created_at': created_at_ms,
            'updated_at': created_at_ms,
            'matches': {},
            'started_swaps': [],
            'uuid': uuid,
            'conf_settings': dict(CONF_SETTINGS),
            'base_orderbook_ticker': BOOK_PAIR[0],
            'rel_orderbook_ticker': BOOK_PAIR[1],
        }
        _broadcast_sse(_sse_payload('NewOrUpdatedItem', uuid))
        return _respond(req, order_result)

    if method == 'cancel_order':
        uuid = params.get('uuid') or req.get('uuid')
        with BOOK_LOCK:
            STATE['book'].pop(uuid, None)
            STATE['takers'].pop(uuid, None)
        _broadcast_sse(_sse_removed(uuid))
        return _respond(req, 'success')

    if method == 'cancel_all_orders':
        with BOOK_LOCK:
            cancelled = list(STATE['book'].keys()) + list(STATE['takers'].keys())
            STATE['book'].clear()
            STATE['takers'].clear()
        for uuid in cancelled:
            _broadcast_sse(_sse_removed(uuid))
        return _respond(req, {'cancelled': cancelled, 'currently_matching': []})

    if method == 'order_status':
        uuid = params.get('uuid') or req.get('uuid')
        with BOOK_LOCK:
            mk = uuid in STATE['book']
            tk = uuid in STATE['takers']
        if mk:
            it = STATE['book'][uuid]
            return _respond(req, {'type': 'Maker', 'order': _maker_value(uuid, it['side'], it['price'], it['base_vol'], it['mine'], it['created_at'])})
        if tk:
            return _respond(req, {'type': 'Taker', 'order': dict(STATE['takers'][uuid])})
        return {'error': 'order not found'}

    if method == 'my_orders':
        # wrapped legacy {result: {maker_orders, taker_orders}} (pinned F2)
        with BOOK_LOCK:
            makers = {
                uuid: _maker_value(uuid, it['side'], it['price'], it['base_vol'], it['mine'], it['created_at'])
                for uuid, it in STATE['book'].items() if it['mine']
            }
            takers = {uuid: dict(v) for uuid, v in STATE['takers'].items()}
        return _respond(req, {'maker_orders': makers, 'taker_orders': takers})

    # ---- swaps (F3) ----------------------------------------------------------
    if method == 'active_swaps':
        include = bool(params.get('include_status') or False)
        with BOOK_LOCK:
            act = {u: dict(s) for u, s in STATE['active'].items()}
        if _is_v2(req):
            statuses = {u: {'swap_type': s['swap_type'], 'swap_data': s['swap_data']}
                        for u, s in act.items()} if include else {}
            return _respond(req, {'uuids': sorted(act), 'statuses': statuses})
        # legacy twin: BARE envelope (no "result" wrapper), statuses is an
        # Option -> null without include_status; include only fills V1 swaps
        # as raw type-tagged SavedSwap (hide_secrets NOT applied there).
        statuses = None
        if include:
            statuses = {u: dict({'type': ('Taker' if act[u]['swap_type'].startswith('Taker')
                                          else 'Maker'), **act[u]['swap_data']})
                        for u in sorted(act) if act[u]['swap_type'].endswith('V1')}
        return {'uuids': sorted(act), 'statuses': statuses}

    if method == 'my_recent_swaps':
        with BOOK_LOCK:
            act = [dict(s) for s in STATE['active'].values()]
            hist = [dict(i) for i in STATE['hist']]
        items = act + hist          # active first, history newest first
        total = len(items)
        limit = max(1, int(params.get('limit') or 10))
        page = max(1, int(params.get('page_number') or 1))
        paging = {'from_uuid': params.get('from_uuid'), 'skipped': 0,
                  'limit': limit, 'total': total, 'page_number': page,
                  'total_pages': 1 if total <= limit else (total + limit - 1) // limit,
                  'found_records': total}
        start = (page - 1) * limit
        items = items[start:start + limit]
        if _is_v2(req):
            return _respond(req, {'swaps': items, **paging})
        # legacy twin: items are MIXED per swap version — V1 items arrive as
        # MySwapStatusResponse (hide_secrets applied), V2 items FLAT (the
        # MySwapForRpc struct, no side tag) — lp_swap.rs:1303-1360.
        legacy_items = []
        for s in items:
            legacy_items.append(_legacy_item(s['swap_type'], s['swap_data'])
                                if s['swap_type'].endswith('V1')
                                else dict(s['swap_data']))
        return {'result': {'swaps': legacy_items, **paging}}

    if method == 'my_swap_status':
        # the uuid lives in params on BOTH envelopes (lp_swap.rs:1111 reads
        # req["params"]["uuid"] even for legacy callers); accept the top
        # level too so hand-rolled curls behave
        uuid = params.get('uuid') or (params.get('params') or {}).get('uuid')
        item = _tagged(uuid)
        if item is None:
            if _is_v2(req):
                return _v2_error('NoSwapWithUuid', 'my_swap_status', uuid)
            return {'error': 'No swap with uuid %s' % (uuid or '')}
        if _is_v2(req):
            return _respond(req, item)
        if item['swap_type'].endswith('V1'):
            return {'result': _legacy_item(item['swap_type'], item['swap_data'])}
        # a V2 swap on the legacy route: FLAT MySwapForRpc (no side tag)
        return {'result': dict(item['swap_data'])}

    if method == 'withdraw' and _is_v2(req):
        return _respond(req, {   # shape pinned live in F1 (v2 withdraw)
            'tx_hex': '0200000000010162mocktxhex0', 'tx_hash': 'ab' * 32,
            'from': [MY_ADDRESS], 'to': [params.get('to', 'be1qunknown')],
            'total_amount': params.get('amount', '0'),
            'spent_by_me': params.get('amount', '0'),
            'my_balance_change': '-' + str(params.get('amount', '0')),
            'received_by_me': '0', 'block_height': 0, 'timestamp': int(time.time()),
            'fee_details': {'amount': '0.00001', 'coin': params.get('coin', 'ELEK')},
        })

    if method == 'send_raw_transaction':
        return _respond(req, {'tx_hash': 'cd' * 32})

    if method == 'stream::orderbook::enable' and _is_v2(req):
        return {'mmrpc': '2.0', 'result': {'streamer_id': STREAMER_ID}, 'id': None}

    # swap_status is a GLOBAL streamer (no uuid/pair suffix — streamer_ids.rs)
    if method == 'stream::swap_status::enable' and _is_v2(req):
        return {'mmrpc': '2.0', 'result': {'streamer_id': 'SWAP_STATUS'}, 'id': None}

    if method == 'stream::disable' and _is_v2(req):
        return {'mmrpc': '2.0', 'result': 'Success', 'id': None}

    return _v2_error('NoSuchMethod', 'dispatcher', {'suggestions': [], 'data': [method]})


# ---- SSE ---------------------------------------------------------------------

SSE_CLIENTS = []   # wfile handles of connected browsers


def _sse_frame(order_type, body):
    return json.dumps({
        '_type': STREAMER_ID,
        'message': {'order_type': order_type, 'order_data': body},
    })


def _sse_payload(order_type, uuid):
    """Rat-serialized OrderbookP2PItem lookalike: created_at SECONDS, volumes
    and price as [[numer...], [denom...]] pairs, no is_mine (live, F2)."""
    with BOOK_LOCK:
        it = STATE['book'].get(uuid)
    if not it:
        return json.dumps({'_type': STREAMER_ID,
                           'message': {'order_type': 'RemovedItem', 'order_data': uuid}})
    rel_vol = str(Decimal(it['base_vol']) * Decimal(it['price']))
    return _sse_frame(order_type, {
        'base': BOOK_PAIR[0],
        'rel': BOOK_PAIR[1],
        'pubkey': MY_PUBKEY if it['mine'] else OTHER_PUBKEY,
        'uuid': uuid,
        'created_at': int(time.time()),
        'price': _rat(it['price']),
        'max_volume': _rat(it['base_vol']),
        'min_volume': _rat(str(Decimal(it['base_vol']) / 10)),
        'rel_max_volume': _rat(rel_vol),
    })


def _sse_removed(uuid):
    return json.dumps({
        '_type': STREAMER_ID,
        'message': {'order_type': 'RemovedItem', 'order_data': uuid},
    })


def _broadcast_sse(json_line):
    dead = []
    with BOOK_LOCK:
        targets = list(SSE_CLIENTS)
    for w, lock in targets:
        try:
            lock.acquire()
            w.write(b'data: ' + json_line.encode() + b'\n\n')
            w.flush()
        except Exception:
            dead.append((w, lock))
        finally:
            lock.release()
    for t in dead:
        try:
            SSE_CLIENTS.remove(t)
        except ValueError:
            pass


def _tidy_client(w, lock):
    """Deregister a client on disconnect (idempotent)."""
    for e in list(SSE_CLIENTS):
        if e[0] is w:
            SSE_CLIENTS.remove(e)


def _ticker():
    while True:
        time.sleep(5)
        if _mode() == 'static':
            continue   # frozen book (headless dumps): no cycle frames
        try:
            _cycle_step()
        except Exception as e:   # keep the ticker alive no matter what
            print('mock: ticker step failed %r' % (e,), flush=True)


# ---- scripted SSE loop ------------------------------------------------------
# One shared ticker thread, one step every 5s: orders vanish and reappear
# (incl. the OWN ask, which exercises the is_mine row and the my_orders
# transition in the UI). All connected clients see the same stream.
A2 = '11e5a1f0-0000-4100-8000-000000000002'   # own ask (is_mine)
B1 = '11e5a1f0-0000-4100-8000-000000000003'


def _cycle():
    while True:   # remove own ask -> re-add it -> flip a bid -> re-add it
        yield ('RemovedItem', A2)
        yield ('NewOrUpdatedItem', A2)
        yield ('RemovedItem', B1)
        yield ('NewOrUpdatedItem', B1)


_CYCLE_GEN = _cycle()


def _cycle_step():
    kind, uuid = next(_CYCLE_GEN)
    with BOOK_LOCK:
        if kind == 'RemovedItem':
            STATE['book'].pop(uuid, None)
        elif uuid in ORIG:   # re-add the pristine record with a fresh timestamp
            STATE['book'][uuid] = dict(ORIG[uuid], created_at=int(time.time() * 1000))
    body = uuid if kind == 'RemovedItem' else \
        json.loads(_sse_payload('NewOrUpdatedItem', uuid))['message']['order_data']
    _broadcast_sse(_sse_frame(kind, body))


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'mock-kdf'

    # ---- helpers -----------------------------------------------------------
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Max-Age', '3600')

    def _send_json(self, code, obj):
        body = (json.dumps(obj) + '\n').encode()
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- verbs -------------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        try:
            req = json.loads(body)
        except Exception:
            self._send_json(400, {'error': 'invalid json'})
            return
        if not isinstance(req, dict):
            self._send_json(400, {'error': 'request must be a json object'})
            return
        resp = _canned(req)
        if isinstance(resp, tuple):     # ('__EMPTY_500__', None)
            # the daemon's real empty-500 shape (activation dead-end form)
            self.send_response(500)
            self._cors()
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if (isinstance(resp, dict) and 'error' in resp and 'result' not in resp
                and resp.get('mmrpc') != '2.0'):
            # legacy RPC-level errors answer 500 WITH a json body — pinned
            # live (the v2 error form is in-band and stays 200)
            self._send_json(500, resp)
            return
        self._send_json(200, resp)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/fx/rates.json':
            # Canned electrs rich-shape snapshot. The real file is written by
            # the electrs fetcher loop and served by elek-web (env
            # MM_WEB_FX_RATES, doc/elektron.md §13); the mock mirrors the
            # regtest book shape so the selftest exercises the same trust gate
            # (prices from BASE_BOOK best levels, fiat = mid * reference).
            self._send_json(200, {
                'ticker': 'ELEK',
                'time': int(time.time()),
                'age_secs': 0,
                'usd': 1.2814,
                'eur': 1.145,
                'usd_per_btc': 86000.0,
                'source': 'p2p_market',
                'market': {
                    'pair': '%s/%s' % BOOK_PAIR,
                    'best_bid': 0.0000098,
                    'best_ask': 0.00002,
                    'mid': 0.0000149,
                    'asks': 2,
                    'bids': 2,
                },
            })
            return
        if parsed.path != '/event-stream':
            self.send_response(404)
            self._cors()
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        client_id = parse_qs(parsed.query).get('id', [''])[0]
        print('mock: SSE client connected id=%s' % client_id, flush=True)
        self.serve_sse()

    # ---- SSE ---------------------------------------------------------------
    def serve_sse(self):
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        # NO initial snapshot (faithful: the real streamer sends none) — the
        # UI's poll render IS the initial view; SSE only adds live polish.
        my_lock = threading.Lock()
        entry = (self.wfile, my_lock)
        SSE_CLIENTS.append(entry)
        try:
            # The ticker thread pushes data to every client; this handler only
            # parks until the browser disconnects, then tidies up.
            while self.rfile.read(8192):
                time.sleep(0.1)
        finally:
            try:
                SSE_CLIENTS.remove(entry)
            except ValueError:
                pass
            _tidy_client(self.wfile, my_lock)

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=7993)
    ap.add_argument('--mode', choices=['ok', 'static', 'err', 'empty', 'slow'], default='ok')
    args = ap.parse_args()
    _set_mode(args.mode)
    print('mock-kdf listening on http://127.0.0.1:%d (mode %s, pair %s/%s)'
          % (args.port, _mode(), *BOOK_PAIR), flush=True)
    threading.Thread(target=_ticker, daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()