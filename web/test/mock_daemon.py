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

Known deviations from the real daemon (kept provisional, marked in code):
- maker_orders value shape (MakerOrderForMyOrdersRpc) is a plausible
  reconstruction (unpinned surface; the UI reads it defensively)
- all books are for the fixed pair ELEK/tBTC; other pairs answer an empty book

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
}


def _record(uuid, side, price, base_vol, mine):
    return {'uuid': uuid, 'side': side, 'price': price, 'base_vol': base_vol,
            'mine': mine, 'created_at': int(time.time() * 1000) - 123456789}


ORIG = {}   # pristine book records (the cycle re-adds from here)
for _u, (_side, _price, _vol, _mine) in BASE_BOOK.items():
    ORIG[_u] = _record(_u, _side, _price, _vol, _mine)
    STATE['book'][_u] = dict(ORIG[_u])   # full initial book incl. the own ask


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

    guard = _mode_guard(req)
    if guard is not None:
        return guard

    coins = [
        {'ticker': 'ELEK', 'coin': 'ELEK', 'address': MY_ADDRESS, 'rpcport': 7796},
        {'ticker': 'tBTC', 'coin': 'tBTC', 'address': 'tb1qmocktbtcaddress00000000000000000000', 'rpcport': 18332},
    ]

    if method == 'version':
        return _respond(req, '3.0.0-beta_mock')

    if method == 'get_enabled_coins':
        return _respond(req, coins)

    if method == 'trade_preimage':
        # v2 shape seen live (taker form pinned in F1/F2): total_fees rows
        # plus required_balance. setprice form unpinned — same generic shape.
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
        # BARE on the real daemon (no "result" wrapper) — do not wrap
        coin = params.get('coin') or req.get('coin')
        if coin == 'ELEK':
            return {'coin': 'ELEK', 'balance': '231.4567',
                    'unspendable_balance': '0', 'address': MY_ADDRESS}
        return {'coin': coin, 'balance': '0.5', 'unspendable_balance': '0',
                'address': coins[1]['address']}

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
        # MAKER: MakerOrderCreated — the order joins the book (via the P2P
        # loopback on the real daemon); SSE listeners get their own frame.
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

    if method == 'active_swaps':
        return _respond(req, {'uuids': [], 'statuses': {}})

    if method == 'my_recent_swaps':
        return _respond(req, {'swaps': [], 'from_uuid': None, 'skipped': 0,
                              'limit': 10, 'total_count': 0})

    if method == 'my_swap_status' and _is_v2(req):
        return _respond(req, {'type': 'Taker', 'events': []})

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

    if method == 'stream::orderbook::enable':
        return {'mmrpc': '2.0', 'result': {'streamer_id': STREAMER_ID}, 'id': None}

    if method == 'stream::disable':
        return {'mmrpc': '2.0', 'result': {}, 'id': None}

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
        self._send_json(200, resp)

    def do_GET(self):
        parsed = urlparse(self.path)
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