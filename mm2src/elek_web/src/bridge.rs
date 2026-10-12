//! HTTP-upgrade splice bridge: the render.com P2P route.
//!
//! render.com terminates TLS at its edge and forwards plain HTTP to the only
//! externally reachable port (10000, served by elek-web). A native kdf node
//! dials its seed there as `/dns4/elektron-market.onrender.com/tcp/443/wss`
//! (mm2_p2p `RelayAddress::WebSocket`) — a websocket handshake riding the
//! HTTPS route through to this server. This module accepts the upgrade and
//! **splices** the bytes to the relay's plain-ws listener
//! (`MM_WEB_P2P_BRIDGE`, e.g. `127.0.0.1:43341`): websocket framing stays
//! end-to-end between the dialing kdf and the relay — elek-web neither parses
//! nor re-frames websocket data, it only replays the handshake once (see the
//! fork patch that opens the plain-ws listener in
//! mm2_p2p/src/behaviours/atomicdex.rs).
//!
//! Why splice instead of a websocket library: the relay already speaks plain
//! websocket (libp2p's ws transport without certs). elek-web copies the
//! client's handshake to the relay (same `Sec-WebSocket-Key`), mirrors the
//! relay's 101 response back verbatim (so `Sec-WebSocket-Accept`, subprotocol
//! and permessage-deflate negotiation stay end-to-end), and from then on the
//! two sides exchange raw bytes through a tokio pump.

use hyper::{
    header::{HeaderMap, HeaderName, HeaderValue, CONNECTION, UPGRADE},
    Body, Method, Request, Response, StatusCode,
};
use std::{net::SocketAddr, time::Duration};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
};

/// Read timeout for the relay's 101 response head. Loopback, no load — the
/// 3 s only guards a wedged relay.
const RELAY_RESPONSE_TIMEOUT: Duration = Duration::from_secs(3);
/// Handshake head size cap (status line + headers).
const HEAD_CAP: usize = 16 * 1024;

/// A websocket upgrade we can splice: GET, `Connection: upgrade`,
/// `Upgrade: websocket`, a `Sec-WebSocket-Key`, version 13. False for every
/// ordinary HTTP request (static files, /rpc, /healthz, ...).
pub(crate) fn is_ws_upgrade(req: &Request<Body>) -> bool {
    if *req.method() != Method::GET {
        return false;
    }
    let connection_is_upgrade = req
        .headers()
        .get(CONNECTION)
        .and_then(|v| v.to_str().ok())
        .map(|v| v.split(',').any(|t| t.trim().eq_ignore_ascii_case("upgrade")))
        .unwrap_or(false);
    if !connection_is_upgrade {
        return false;
    }
    let upgrade_is_ws = req
        .headers()
        .get(UPGRADE)
        .and_then(|v| v.to_str().ok())
        .map(|v| v.eq_ignore_ascii_case("websocket"))
        .unwrap_or(false);
    if !upgrade_is_ws {
        return false;
    }
    if req.headers().get("sec-websocket-key").is_none() {
        return false;
    }
    req.headers().get("sec-websocket-version").and_then(|v| v.to_str().ok()) == Some("13")
}

/// The upgrade request replayed to the relay: the client's own path and
/// websocket negotiation headers with the host rewritten to the relay target.
/// The `Sec-WebSocket-Key` passes through untouched, so the relay's
/// `Sec-WebSocket-Accept` (mirrored to the client below) is the one computed
/// over the CLIENT's key — the client's own handshake verification succeeds.
fn relay_upgrade_request(req: &Request<Body>, relay_host: &str) -> Vec<u8> {
    let mut out = format!(
        "GET {} HTTP/1.1\r\nHost: {}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n",
        req.uri().path_and_query().map(|pq| pq.as_str()).unwrap_or("/"),
        relay_host,
    )
    .into_bytes();
    for (name, value) in req.headers().iter() {
        // Host/Connection/Upgrade are rebuilt above; a GET upgrade carries no
        // body, so length/encoding headers must not leak either.
        let is_handled = matches!(
            name.as_str(),
            "host" | "connection" | "upgrade" | "content-length" | "transfer-encoding"
        );
        if is_handled {
            continue;
        }
        out.extend_from_slice(format!("{}: {}\r\n", name, value.to_str().unwrap_or_default()).as_bytes());
    }
    out.extend_from_slice(b"\r\n");
    out
}

/// Read and parse one HTTP response head ("\r\n\r\n" terminated) from the
/// relay. Returns (status_code, headers, leftover_bytes). Anything but 101 is
/// rejected by the caller: the relay plain-ws listener must accept our
/// replayed handshake — this bridge is not a generic HTTP proxy. The read
/// chunks 512 bytes at a time, so a relay frame coalesced into the same
/// segment can ride in past the head end; those bytes belong to the client —
/// callers must forward `leftover` before pumping, never drop them.
async fn relay_response_head(stream: &mut TcpStream) -> Result<(u16, HeaderMap, Vec<u8>), String> {
    let mut buf = Vec::with_capacity(512);
    let mut chunk = [0u8; 512];
    loop {
        if buf.len() > HEAD_CAP {
            return Err(format!("relay response head exceeds {} bytes", HEAD_CAP));
        }
        let n = tokio::time::timeout(RELAY_RESPONSE_TIMEOUT, stream.read(&mut chunk))
            .await
            .map_err(|_| "relay response timed out".to_string())?
            .map_err(|e| e.to_string())?;
        if n == 0 {
            return Err("relay closed before responding".to_string());
        }
        buf.extend_from_slice(&chunk[..n]);
        if let Some(end) = find_head_end(&buf) {
            let head = String::from_utf8_lossy(&buf[..end]).into_owned();
            let mut lines = head.lines();
            let status_line = lines.next().ok_or_else(|| "empty relay response".to_string())?;
            let code: u16 = status_line
                .split_whitespace()
                .nth(1)
                .and_then(|c| c.parse().ok())
                .ok_or_else(|| format!("unparseable relay status line: {:?}", status_line))?;
            let mut headers = HeaderMap::new();
            for line in lines {
                if let Some((name, value)) = line.split_once(':') {
                    let name = HeaderName::from_bytes(name.trim().as_bytes())
                        .map_err(|e| format!("bad header name {:?}: {}", name, e))?;
                    let value = HeaderValue::from_str(value.trim())
                        .map_err(|e| format!("bad header value {:?}: {}", value, e))?;
                    headers.append(name, value);
                }
            }
            let leftover = buf[end..].to_vec();
            return Ok((code, headers, leftover));
        }
    }
}

/// Index of the "\r\n\r\n" terminator in a buffered head, if complete.
fn find_head_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n").map(|p| p + 4)
}

/// Accept a request that `is_ws_upgrade` already recognized: dial the relay,
/// replay the handshake with the client's key, mirror the relay's 101, then
/// pump both directions until either side closes.
pub(crate) async fn splice(mut req: Request<Body>, bridge_addr: SocketAddr) -> Response<Body> {
    let relay_host = bridge_addr.to_string();
    let mut relay = match TcpStream::connect(bridge_addr).await {
        Ok(stream) => stream,
        Err(e) => {
            return bad_gateway(&format!("relay unreachable at {}: {}", bridge_addr, e));
        },
    };
    if let Err(e) = relay.write_all(&relay_upgrade_request(&req, &relay_host)).await {
        return bad_gateway(&format!("relay handshake write failed: {}", e));
    }
    let (code, relay_headers, leftover) = match relay_response_head(&mut relay).await {
        Ok(ok) => ok,
        Err(e) => return bad_gateway(&format!("relay handshake failed: {}", e)),
    };
    if code != 101 {
        return bad_gateway(&format!("relay refused the upgrade (HTTP {})", code));
    }

    // Mirror the relay's 101 verbatim (its Sec-WebSocket-Accept is computed
    // over the client's own key, which we replayed), then splice. hyper moves
    // the connection into upgrade mode on the 101 and hands the raw IO to the
    // pump; the response must be returned for that to happen.
    let mut resp = Response::new(Body::empty());
    *resp.status_mut() = StatusCode::SWITCHING_PROTOCOLS;
    *resp.headers_mut() = relay_headers;
    println!("elek-web: ws splice -> {} ({})", bridge_addr, req.uri().path());

    tokio::spawn(async move {
        match hyper::upgrade::on(&mut req).await {
            Ok(mut client) => {
                // relay frames that coalesced into the 101 head chunk arrive
                // BEFORE copy_bidirectional can start mirroring — push them
                // through first or the client loses the start of the stream.
                if !leftover.is_empty() {
                    if let Err(e) = tokio::io::AsyncWriteExt::write_all(&mut client, &leftover).await {
                        eprintln!("elek-web: ws bridge coalesced-forward failed: {}", e);
                        return;
                    }
                }
                match tokio::io::copy_bidirectional(&mut client, &mut relay).await {
                    Ok((to_relay, to_client)) => println!(
                        "elek-web: ws bridge -> {} closed ({} bytes to relay, {} bytes to client)",
                        bridge_addr, to_relay, to_client
                    ),
                    Err(e) => eprintln!("elek-web: ws bridge pump error: {}", e),
                }
            },
            Err(e) => eprintln!("elek-web: ws upgrade never completed: {}", e),
        }
    });
    resp
}

fn bad_gateway(msg: &str) -> Response<Body> {
    let mut resp = Response::new(Body::from(format!("p2p bridge error: {}\n", msg)));
    *resp.status_mut() = StatusCode::BAD_GATEWAY;
    resp
}
