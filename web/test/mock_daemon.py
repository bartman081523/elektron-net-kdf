#!/usr/bin/env python3
"""Mock kdf daemon for UI iteration without a chain.

Stdlib only. Canned responses mirror the envelopes verified live in phases
1-6 (scripts/elektron/testnet_rpc.py, doc/elektron.md sections 9-10):

- legacy top-level: {"userpass", "method", ...} -> {"result": ...} (or the
  legacy {"error": "<string>"} form)
- v2: {"mmrpc": "2.0", "method", "params"} -> {"mmrpc": "2.0", "result": ...}
  or {"mmrpc": "2.0", "error": {error_path, error_type, error_trace, error_data}}
- GET /event-stream?id=<n> -> SSE, data: {"_type": "ORDERBOOK_UPDATE:orbk:ELEK:tBTC",
  "message": {order_type, order_data}} with a scripted update loop.

SSE order_data carries NO is_mine (faithful to OrderbookP2PItem): the UI
derives own-orders by pubkey match. RemovedItem's order_data is the bare uuid.

PROVISIONAL shapes (plausible, pinned against a real daemon in phase F1):
my_orders, sell/buy, cancel_order, setprice, trade_preimage, withdraw,
stream::*. Everything else is shape-faithful.

Modes (POST {"method": "mock.set_mode", "params": {"mode": ...}}):
  ok    - canned success (default)
  err   - every RPC method answers the v2 error form
  empty - every RPC method answers HTTP 500 with an EMPTY body
  slow  - every RPC method answers after 2s

Usage:  python3 web/test/mock_daemon.py [--port 7993] [--mode ok]
CORS is wide open (*) so the SPA on http://localhost:3000 can talk to it.
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

MODE_LOCK = threading.Lock()
MODE = {'value': 'ok'}

BOOK_LOCK = threading.Lock()
BOOK_PAIR = ('ELEK', 'tBTC')   # already alphabetically sorted (orbk topic form)
BOOK_UUIDS = {
    'ask1': '11e5a1f0-0000-4100-8000-000000000001',
    'ask2': '11e5a1f0-0000-4100-8000-000000000002',
    'bid1': '11e5a1f0-0000-4100-8000-000000000003',
    'bid2': '11e5a1f0-0000-4100-8000-000000000004',
}
MY_PUBKEY = 'aa11' * 16
OTHER_PUBKEY = 'bb22' * 16
MY_ADDRESS = 'be1qmockmyaddress0000000000000000000000000000'
OTHER_ADDRESS = 'be1qmockotheraddress00000000000000000000000'

# uuid -> (side, price, volume, is_mine)
BASE_BOOK = {
    BOOK_UUIDS['ask1']: ('ask', '0.00002', '400', False),
    BOOK_UUIDS['ask2']: ('ask', '0.0000215', '60', True),        # own order
    BOOK_UUIDS['bid1']: ('bid', '0.0000098', '1200', False),
    BOOK_UUIDS['bid2']: ('bid', '0.0000089', '77', False),
}


def _item(uuid, side, price, vol, mine):
    # faithful to OrderbookP2PItem + the is_mine of the legacy orderbook RPC
    return {
        'uuid': uuid,
        'pubkey': MY_PUBKEY if mine else OTHER_PUBKEY,
        'address': MY_ADDRESS if mine else OTHER_ADDRESS,
        'base': BOOK_PAIR[0],
        'rel': BOOK_PAIR[1],
        'price': price,
        'max_volume': vol,
        'min_volume': '1',
        'created_at': int(time.time() * 1000) - 123456789,
        'is_mine': mine,
    }


def _book():
    return {u: _item(u, *args) for u, args in BASE_BOOK.items()}


STATE = {
    'book': _book(),
    # uuid -> 'ask'|'bid' (the SSE payload has no side field, so the mock
    # derives asks/bids from this map, like the UI must)
    'side': {u: args[0] for u, args in BASE_BOOK.items()},
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
    return {'mmrpc': '2.0', 'error': err}


def _is_v2(req):
    return isinstance(req, dict) and req.get('mmrpc') == '2.0'


def _respond(req, result):
    # a v2 request gets the v2 success form, a legacy request the legacy form
    if _is_v2(req):
        return {'mmrpc': '2.0', 'result': result}
    return {'result': result}


def _side(uuid):
    return STATE['side'].get(uuid, 'ask')


def _sorted_book():
    """(asks, bids) sorted the way the real orderbook RPC sorts them."""
    with BOOK_LOCK:
        items = list(STATE['book'].values())
    asks = sorted((i for i in items if _side(i['uuid']) == 'ask'),
                  key=lambda i: float(i['price']))
    bids = sorted((i for i in items if _side(i['uuid']) == 'bid'),
                  key=lambda i: -float(i['price']))
    return asks, bids


def _new_order_uuid():
    with BOOK_LOCK:
        u = '11e5a1f0-0000-4100-8000-%012d' % (len(STATE['book']) + 1)
    return u


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

    mode = _mode()
    if mode == 'err':
        return _v2_error('MockError', 'mock', 'mode=err: every method fails')
    if mode == 'empty':
        return ('__EMPTY_500__', None)   # sentinel handled by the caller
    if mode == 'slow':
        time.sleep(2)

    coins = [
        {'ticker': 'ELEK', 'coin': 'ELEK', 'address': MY_ADDRESS, 'rpcport': 7796},
        {'ticker': 'tBTC', 'coin': 'tBTC', 'address': 'tb1qmocktbtcaddress00000000000000000000', 'rpcport': 18332},
    ]

    if method == 'version':
        return _respond(req, '3.0.0-beta_mock')

    if method == 'get_enabled_coins':
        return _respond(req, coins)

    if method == 'trade_preimage':
        # PROVISIONAL shape - pinned in F1
        return _respond(req, {
            'base': params.get('base'), 'rel': params.get('rel'),
            'swap_trade_fee': {'amount': '0.001', 'coin': 'ELEK'},
            'fee_details': {'base': {'amount': '0.00001', 'coin': 'ELEK'},
                            'rel': {'amount': '0.00002', 'coin': 'tBTC'}},
            'volume': params.get('volume'),
        })

    if method == 'my_balance':
        coin = params.get('coin') or req.get('coin')
        if coin == 'ELEK':
            return _respond(req, {'coin': 'ELEK', 'balance': '231.4567',
                                  'unspendable_balance': '0', 'address': MY_ADDRESS})
        return _respond(req, {'coin': coin, 'balance': '0.5', 'unspendable_balance': '0',
                              'address': coins[1]['address']})

    if method == 'orderbook':
        asks, bids = _sorted_book()
        return _respond(req, {
            'base': BOOK_PAIR[0], 'rel': BOOK_PAIR[1],
            'num_asks': len(asks), 'num_bids': len(bids),
            'asks': asks, 'bids': bids,
        })

    if method in ('sell', 'buy'):
        if _is_v2(req):
            # faithful: this build has NO v2 sell/buy
            return _v2_error('NoSuchMethod', 'dispatcher', {'data': [method]})
        side = 'ask' if method == 'sell' else 'bid'
        with BOOK_LOCK:
            uuid = _new_order_uuid()
            STATE['book'][uuid] = _item(uuid, side, params.get('price', '0.00001'),
                                        params.get('volume', '1'), True)
            STATE['side'][uuid] = side
        return _respond(req, uuid)   # PROVISIONAL shape - pinned in F1

    if method == 'setprice':
        if _is_v2(req):
            return _v2_error('NoSuchMethod', 'dispatcher', {'data': [method]})
        side = 'ask'
        with BOOK_LOCK:
            uuid = _new_order_uuid()
            STATE['book'][uuid] = _item(uuid, side, params.get('price', '0.00001'),
                                        params.get('volume', '10'), True)
            STATE['side'][uuid] = side
        return _respond(req, uuid)   # PROVISIONAL shape - pinned in F1

    if method == 'cancel_order':
        uuid = params.get('uuid') or req.get('uuid')
        with BOOK_LOCK:
            STATE['book'].pop(uuid, None)
        return _respond(req, {'uuid': uuid, 'canceled': True})   # PROVISIONAL

    if method == 'cancel_all_orders':
        with BOOK_LOCK:
            n = len(STATE['book'])
            STATE['book'].clear()
        return _respond(req, {'canceled': ['all(%d)' % n]})      # PROVISIONAL

    if method == 'my_orders':
        with BOOK_LOCK:
            items = list(STATE['book'].values())
            mine = [i for i in items if i['is_mine']]
        # PROVISIONAL shape - pinned in F1
        return _respond(req, {'type': 'my_orders', 'orders': mine})

    if method == 'active_swaps':
        return _respond(req, {'uuids': [], 'statuses': {}})

    if method == 'my_recent_swaps':
        return _respond(req, {'swaps': [], 'from_uuid': None, 'skipped': 0,
                              'limit': 10, 'total_count': 0})

    if method == 'my_swap_status' and _is_v2(req):
        return _respond(req, {'type': 'Taker', 'events': []})

    if method == 'withdraw' and _is_v2(req):
        return _respond(req, {   # PROVISIONAL shape - pinned in F1
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
        return _respond(req, {'success': True})  # PROVISIONAL

    if method == 'stream::disable':
        return _respond(req, {})                 # PROVISIONAL

    return _v2_error('NoSuchMethod', 'dispatcher', {'suggestions': [], 'data': [method]})

    # unreachable


def _sse_payload(order_type, item):
    return json.dumps({
        '_type': 'ORDERBOOK_UPDATE:orbk:%s:%s' % BOOK_PAIR,
        'message': {'order_type': order_type,
                    'order_data': {k: v for k, v in item.items() if k != 'is_mine'}},
    })


def _sse_removed(uuid):
    return json.dumps({
        '_type': 'ORDERBOOK_UPDATE:orbk:%s:%s' % BOOK_PAIR,
        'message': {'order_type': 'RemovedItem', 'order_data': uuid},
    })


# ---- scripted SSE loop ------------------------------------------------------
# One step every 5s, cycling: prices move, orders vanish and reappear.
def _scripted(step):
    if step == 'update_a1':
        return _item(BOOK_UUIDS['ask1'], 'ask', '0.000019', '400', False)
    if step == 'remove_a2':
        return BOOK_UUIDS['ask2']
    if step == 'add_a2':
        return _item(BOOK_UUIDS['ask2'], 'ask', '0.0000215', '60', True)
    if step == 'update_b1':
        return _item(BOOK_UUIDS['bid1'], 'bid', '0.0000098', '1300', False)
    if step == 'remove_b2':
        return BOOK_UUIDS['bid2']
    if step == 'add_b2':
        return _item(BOOK_UUIDS['bid2'], 'bid', '0.0000089', '77', False)
    raise AssertionError('unknown step ' + step)


CYCLE = ['update_a1', 'remove_a2', 'add_a2', 'update_b1', 'remove_b2', 'add_b2']


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
        try:
            # initial snapshot: every item as NewOrUpdatedItem
            with BOOK_LOCK:
                items = list(STATE['book'].values())
            for it in items:
                self.wfile.write(b'data: ' + _sse_payload('NewOrUpdatedItem', it).encode() + b'\n\n')
            self.wfile.flush()
            i = 0
            while True:
                time.sleep(5)
                step = CYCLE[i % len(CYCLE)]
                i += 1
                payload = _scripted(step)
                with BOOK_LOCK:
                    if isinstance(payload, str):        # remove
                        STATE['book'].pop(payload, None)
                        self.wfile.write(b'data: ' + _sse_removed(payload).encode() + b'\n\n')
                    else:                               # update / (re-)add
                        STATE['book'][payload['uuid']] = payload
                        self.wfile.write(b'data: ' + _sse_payload('NewOrUpdatedItem', payload).encode() + b'\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=7993)
    ap.add_argument('--mode', choices=['ok', 'err', 'empty', 'slow'], default='ok')
    args = ap.parse_args()
    _set_mode(args.mode)
    print('mock-kdf listening on http://127.0.0.1:%d (mode %s, pair %s/%s)'
          % (args.port, _mode(), *BOOK_PAIR), flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()