//! elek-web: serves the market web UI (`web/` at the repo root) on loopback so
//! the browser can talk to the local kdf daemon directly (CORS handled by the
//! daemon's `rpccors`; see doc/elektron.md section 11).
//!
//! Deliberately dependency-minimal (hyper + tokio, nothing else): static
//! files come from disk (no embedding crates in the workspace), the SPA uses
//! hash routing so only real files exist under MM_WEB_ROOT.
//!
//! Auto-connect: when BOTH MM_WEB_RPC_URL and MM_WEB_RPC_PASS are set, the
//! SPA's boot fetch picks them up at CONFIG_PATH and connects without the
//! connect form. Trust note: that hands the rpc password to every browser
//! that can reach this server (loopback / ssh-tunnelled) — same machine-local
//! trust level as the .env itself; keep the bind on loopback and
//! `/elek-web-config.json` is only served when both variables are set
//! (otherwise 404, and the SPA falls back to the connect form).
//!
//! Proxy mode (container deployments, e.g. render.com where the SPA has no
//! other route to a daemon): MM_WEB_PROXY_URL + MM_WEB_RPC_PASS turn this
//! server into a same-origin reverse proxy for ONE local daemon. The SPA
//! connects to "/rpc", POSTs are forwarded with the real rpc password injected
//! server-side, GET /rpc/event-stream streams the daemon's SSE, /healthz
//! proves the daemon is alive. The browser never receives the password:
//! `/elek-web-config.json` handoff says rpc_proxy:true with an empty rpc_pass.
//! Precedence: proxy mode replaces the direct auto_config handoff (a config
//! that would leak MM_WEB_RPC_PASS to browsers), but static files keep
//! working either way.

use hyper::{
    body::to_bytes as body_to_bytes,
    client::HttpConnector,
    header::{
        self, HeaderValue, ALLOW, CACHE_CONTROL, CONTENT_LENGTH, CONTENT_TYPE,
    },
    service::{make_service_fn, service_fn},
    Body, Client, Method, Request, Response, Server, StatusCode,
};
use std::{
    convert::Infallible,
    env,
    net::SocketAddr,
    path::{Path, PathBuf},
};

const DEFAULT_ROOT: &str = "web";
const DEFAULT_ADDR: &str = "127.0.0.1:3000";
const CONFIG_PATH: &str = "/elek-web-config.json";
const FX_RATES_PATH: &str = "/fx/rates.json";

/// File extension to MIME. Self-maintained: the workspace has no mime_guess /
/// rust-embed / include_dir (checked at plan time), and `.mjs` must NOT be
/// served as octet-stream or the browser will refuse the ES module.
fn content_type(path: &Path) -> &'static str {
    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    match ext.as_str() {
        "html" | "htm" => "text/html; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "mjs" | "js" => "text/javascript; charset=utf-8",
        "json" | "map" => "application/json",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "ico" => "image/x-icon",
        "txt" | "md" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}

/// Map a URI path to a file inside `root_canonical`. Returns None on anything
/// suspicious: percent-encoding, backslashes, empty or `..` segments,
/// non-canonical targets (symlink/`..` tricks via the canonicalize check),
/// directories (no listing) and missing files.
fn resolve(root_canonical: &Path, uri_path: &str) -> Option<PathBuf> {
    if uri_path.contains('%') || uri_path.contains('\\') {
        return None;
    }
    let rel = uri_path.trim_start_matches('/');
    let rel = if rel.is_empty() { "index.html" } else { rel };
    if rel.split('/').any(|seg| seg.is_empty() || seg == "." || seg == "..") {
        return None;
    }
    let candidate = root_canonical.join(rel);
    let canonical = candidate.canonicalize().ok()?;
    if !canonical.starts_with(root_canonical) {
        return None;
    }
    if canonical.is_dir() {
        return None;
    }
    Some(canonical)
}

fn base_headers(resp: &mut Response<Body>, content_type: &'static str) {
    resp.headers_mut()
        .insert(CONTENT_TYPE, HeaderValue::from_static(content_type));
    // The UI iterates; serving through systemd means restarts pick up edits
    // only when nothing is cached.
    resp.headers_mut()
        .insert(CACHE_CONTROL, HeaderValue::from_static("no-cache"));
}

fn not_found() -> Response<Body> {
    let mut resp = Response::new(Body::from("not found\n"));
    *resp.status_mut() = StatusCode::NOT_FOUND;
    base_headers(&mut resp, "text/plain; charset=utf-8");
    resp
}

/// Escape one string as a JSON string body (quotes, backslashes, control
/// bytes). Enough for hand-rolled config values — elek_web has no serde.
fn json_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

/// The auto-connect handoff: the SPA's boot fetch reads these two fields and
/// connects without the connect form.
fn config_response(rpc_url: &str, rpc_pass: &str, is_head: bool) -> Response<Body> {
    let body = format!(
        "{{\"rpc_url\":\"{}\",\"rpc_pass\":\"{}\"}}",
        json_escape(rpc_url),
        json_escape(rpc_pass),
    );
    let mut resp = Response::new(if is_head { Body::empty() } else { Body::from(body) });
    base_headers(&mut resp, "application/json");
    resp
}

const RPC_PATH: &str = "/rpc";
const RPC_PATH_SSE: &str = "/rpc/event-stream";

/// Container/deploy-mode proxy target. The SPA talks to THIS server only (same
/// origin); JSON-RPC POSTs and the SSE stream are proxied to ONE local daemon
/// and the rpc password is injected server-side, so no browser ever holds it.

#[derive(Clone)]
struct Proxy {
    /// kdf RPC endpoint as "host:port" (http only — the daemon has no TLS; in
    /// container deployments both run on 127.0.0.1)
    upstream: String,
    /// daemon rpc_password, written into every proxied JSON-RPC body
    pass: String,
}

fn bad_gateway(msg: &str) -> Response<Body> {
    let mut resp = Response::new(Body::from(format!("kdf proxy error: {}\n", msg)));
    *resp.status_mut() = StatusCode::BAD_GATEWAY;
    resp
}

fn bad_request(msg: &str) -> Response<Body> {
    let mut resp = Response::new(Body::from(format!("bad request: {}\n", msg)));
    *resp.status_mut() = StatusCode::BAD_REQUEST;
    resp
}

/// Parse MM_WEB_PROXY_URL ("http://host:port" of the kdf RPC, expected on
/// loopback). The daemon RPC is plain http; other schemes are rejected.
fn parse_proxy_url(raw: &str) -> (String, u16) {
    let rest = raw.trim().trim_end_matches('/');
    let rest = rest.strip_prefix("http://").unwrap_or_else(|| {
        panic!("MM_WEB_PROXY_URL must be http://host:port, got {:?}", raw);
    });
    let (host, port) = match rest.rsplit_once(':') {
        Some((host, port)) => {
            let port: u16 = port
                .parse()
                .unwrap_or_else(|_| panic!("MM_WEB_PROXY_URL: bad port in {:?}", raw));
            (host, port)
        }
        None => panic!("MM_WEB_PROXY_URL: host only, no port: {:?}", raw),
    };
    if host.is_empty() || host.contains(['/', '\\']) {
        panic!("MM_WEB_PROXY_URL: bad host in {:?}", raw);
    }
    (host.to_string(), port)
}

/// Proxy-mode handoff for the SPA boot fetch: relative same-origin base, no
/// password — the browser never learns the daemon password; POSTs to /rpc get
/// it injected server-side.
fn config_proxy_response(is_head: bool) -> Response<Body> {
    let body = "{\"rpc_url\":\"/rpc\",\"rpc_pass\":\"\",\"rpc_proxy\":true}";
    let mut resp = Response::new(if is_head { Body::empty() } else { Body::from(body) });
    base_headers(&mut resp, "application/json");
    resp
}

/// JSON-RPC POST through the proxy: parse the body, force `userpass` to the
/// real daemon password (whatever the browser sent is overwritten), forward to
/// the kdf root. Status and content-type pass through unchanged.
async fn proxy_rpc(rpc: &Client<HttpConnector>, proxy: &Proxy, req: Request<Body>) -> Response<Body> {
    let bytes = match body_to_bytes(req.into_body()).await {
        Ok(b) => b,
        Err(_) => return bad_request("unreadable body"),
    };
    let mut val = match serde_json::from_slice::<serde_json::Value>(&bytes) {
        Ok(v) => v,
        Err(e) => return bad_request(&format!("body is not JSON: {}", e)),
    };
    match val.as_object_mut() {
        Some(obj) => {
            obj.insert("userpass".to_string(), serde_json::Value::from(proxy.pass.as_str()));
        }
        None => return bad_request("body must be a JSON object"),
    };
    let upstream = match Request::builder()
        .method("POST")
        .uri(format!("http://{}/", proxy.upstream))
        .header(CONTENT_TYPE, HeaderValue::from_static("application/json"))
        .body(Body::from(val.to_string()))
    {
        Ok(r) => r,
        Err(e) => return bad_gateway(&format!("build failed: {}", e)),
    };
    match rpc.request(upstream).await {
        Ok(resp) => {
            let (parts, body) = resp.into_parts();
            let mut out = Response::new(body);
            *out.status_mut() = parts.status;
            if let Some(ct) = parts.headers.get(CONTENT_TYPE) {
                out.headers_mut().insert(CONTENT_TYPE, ct.clone());
            }
            out
        }
        Err(e) => bad_gateway(&e.to_string()),
    }
}

/// SSE passthrough: GET /rpc/event-stream?id=<u64> forwards to the daemon's
/// GET /event-stream?id=<u64> with the query intact (enabling a stream is a
/// separate RPC that arrives like any other POST). The client's response body
/// is streamed through unbuffered.
async fn proxy_sse(rpc: &Client<HttpConnector>, proxy: &Proxy, req: &Request<Body>) -> Response<Body> {
    let query = req.uri().query().unwrap_or("");
    let upstream = match Request::builder()
        .method("GET")
        .uri(format!("http://{}/event-stream?{}", proxy.upstream, query))
        .body(Body::empty())
    {
        Ok(r) => r,
        Err(e) => return bad_gateway(&format!("build failed: {}", e)),
    };
    match rpc.request(upstream).await {
        Ok(resp) => {
            let (parts, body) = resp.into_parts();
            let mut out = Response::new(body);
            *out.status_mut() = parts.status;
            for name in [CONTENT_TYPE, CACHE_CONTROL] {
                if let Some(v) = parts.headers.get(&name) {
                    out.headers_mut().insert(name, v.clone());
                }
            }
            out
        }
        Err(e) => bad_gateway(&e.to_string()),
    }
}

/// Deployed-mode health check: probe the daemon through the proxy — 200 only
/// when the local kdf answers a version call with a result.
async fn healthz(rpc: &Client<HttpConnector>, proxy: &Proxy) -> Response<Body> {
    let body = serde_json::json!({ "method": "version", "userpass": proxy.pass });
    let upstream = match Request::builder()
        .method("POST")
        .uri(format!("http://{}/", proxy.upstream))
        .header(CONTENT_TYPE, HeaderValue::from_static("application/json"))
        .body(Body::from(body.to_string()))
    {
        Ok(r) => r,
        Err(e) => return bad_gateway(&format!("build failed: {}", e)),
    };
    let (status, note) = match rpc.request(upstream).await {
        Ok(resp) => {
            let (parts, body) = resp.into_parts();
            let ok = parts.status.is_success()
                && body_to_bytes(body)
                    .await
                    .ok()
                    .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok())
                    .map(|v| v.get("result").is_some())
                    .unwrap_or(false);
            (
                if ok { StatusCode::OK } else { StatusCode::SERVICE_UNAVAILABLE },
                if ok {
                    "ok".to_string()
                } else {
                    format!("kdf unhealthy: HTTP {} without result", parts.status.as_u16())
                },
            )
        }
        Err(e) => (StatusCode::SERVICE_UNAVAILABLE, format!("kdf unreachable: {}", e)),
    };
    let mut resp = Response::new(Body::from(format!("elek-web healthz: {}\n", note)));
    *resp.status_mut() = status;
    resp
}

fn not_allowed() -> Response<Body> {
    let mut resp = Response::new(Body::from(
        "only GET and HEAD are served; JSON-RPC goes to the kdf daemon, not here\n",
    ));
    *resp.status_mut() = StatusCode::METHOD_NOT_ALLOWED;
    resp.headers_mut()
        .insert(ALLOW, HeaderValue::from_static("GET, HEAD"));
    base_headers(&mut resp, "text/plain; charset=utf-8");
    resp
}

async fn send_file(path: PathBuf, is_head: bool) -> Response<Body> {
    match tokio::fs::read(&path).await {
        Ok(bytes) => {
            let mut resp = Response::new(if is_head {
                Body::empty()
            } else {
                Body::from(bytes)
            });
            base_headers(&mut resp, content_type(&path));
            if is_head {
                if let Ok(meta) = tokio::fs::metadata(&path).await {
                    if let Ok(len) = meta.len().to_string().try_into() {
                        resp.headers_mut().insert(CONTENT_LENGTH, len);
                    }
                }
            }
            resp
        }
        Err(_) => not_found(),
    }
}

async fn handle(
    req: Request<Body>,
    root: PathBuf,
    auto_config: Option<(String, String)>,
    fx_rates: Option<PathBuf>,
    proxy: Option<Proxy>,
    rpc: Client<HttpConnector>,
) -> Result<Response<Body>, Infallible> {
    let is_head = matches!(*req.method(), Method::HEAD);
    let method = req.method().clone();
    let path = req.uri().path().to_string();
    let mut resp = match *req.method() {
        Method::GET | Method::HEAD => {
            if path == CONFIG_PATH {
                match &proxy {
                    Some(_) => config_proxy_response(is_head),
                    None => match &auto_config {
                        Some((rpc_url, rpc_pass)) => config_response(rpc_url, rpc_pass, is_head),
                        None => not_found(),
                    },
                }
            } else if path == "/healthz" {
                match &proxy {
                    Some(p) => healthz(&rpc, p).await,
                    None => not_found(),
                }
            } else if path == RPC_PATH_SSE {
                match &proxy {
                    Some(p) => proxy_sse(&rpc, p, &req).await,
                    None => not_found(),
                }
            } else if path == FX_RATES_PATH {
                // electrs' rich-shape rates file (doc/elektron.md §13 / §fx):
                // served same-origin so the SPA can show the unified price
                // estimate without any other origin. No file configured (or the
                // file gone) stays a 404 - the SPA hides the rate line, it never
                // fabricates one.
                match &fx_rates {
                    Some(file) => send_file(file.clone(), is_head).await,
                    None => not_found(),
                }
            } else {
                match resolve(&root, &path) {
                    Some(file) => send_file(file, is_head).await,
                    None => not_found(),
                }
            }
        }
        Method::POST => match (&proxy, path.as_str()) {
            (Some(p), RPC_PATH) | (Some(p), "/rpc/") => proxy_rpc(&rpc, p, req).await,
            _ => not_allowed(),
        },
        _ => not_allowed(),
    };
    resp.headers_mut().insert(
        header::HeaderName::from_static("x-elek-web"),
        HeaderValue::from_static("1"),
    );
    println!("elek-web: {} {} -> {}", method, path, resp.status().as_u16());
    Ok(resp)
}

#[tokio::main]
async fn main() {
    let root_arg = env::var("MM_WEB_ROOT").unwrap_or_else(|_| DEFAULT_ROOT.to_string());
    let addr_arg = env::var("MM_WEB_ADDR").unwrap_or_else(|_| DEFAULT_ADDR.to_string());
    let addr: SocketAddr = addr_arg
        .parse()
        .unwrap_or_else(|_| panic!("MM_WEB_ADDR must parse as ip:port, got {:?}", addr_arg));
    let root = PathBuf::from(&root_arg);
    let root_canonical = root
        .canonicalize()
        .unwrap_or_else(|e| panic!("MM_WEB_ROOT {:?} does not exist: {}", root_arg, e));
    // Auto-connect handoff: only complete pairs enable the endpoint. The value
    // handed over is read at startup, so MM_WEB_RPC_URL is normalized here
    // (no trailing slashes; the SPA's probe() POSTs to the bare root).
    // Proxy mode (container deployments): MM_WEB_PROXY_URL points at the local
    // kdf RPC; MM_WEB_RPC_PASS is used ONLY server-side (injected into proxied
    // calls) — this takes precedence over the direct auto_config handoff,
    // because the direct handoff would give the password to every visitor.
    let proxy: Option<Proxy> = match (env::var("MM_WEB_PROXY_URL"), env::var("MM_WEB_RPC_PASS")) {
        (Ok(url), Ok(pass)) if !url.trim().is_empty() && !pass.is_empty() => {
            let (host, port) = parse_proxy_url(&url);
            println!(
                "elek-web: proxy mode -> JSON-RPC + SSE under {} (same origin), rpc password injected server-side, daemon http://{}:{}",
                RPC_PATH, host, port
            );
            Some(Proxy { upstream: format!("{}:{}", host, port), pass })
        }
        (Ok(_), Ok(_)) => {
            println!("elek-web: MM_WEB_PROXY_URL/MM_WEB_RPC_PASS pair incomplete -> proxy mode off");
            None
        }
        _ => None,
    };

    let rpc: Client<HttpConnector> = Client::new();
    let auto_config: Option<(String, String)> =
        match (env::var("MM_WEB_RPC_URL"), env::var("MM_WEB_RPC_PASS")) {
            (Ok(url), Ok(pass)) => {
                let url = url.trim().trim_end_matches('/').to_string();
                if url.is_empty() {
                    None
                } else {
                    Some((url, pass))
                }
            }
            _ => None,
        };

    // Unified price estimate: elek-web serves the electrs FX rates file
    // same-origin at /fx/rates.json (electrs = single point of responsibility,
    // see doc/elektron.md §13 and electrs src/fx.rs). The file is written by
    // the electrs fetcher loop; elek-web only serves it from disk. Unset env or
    // a missing file degrades to a 404 - nothing is invented at this layer.
    let fx_rates: Option<PathBuf> = env::var("MM_WEB_FX_RATES")
        .ok()
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
        .map(PathBuf::from);

    println!(
        "elek-web: serving {} on http://{} (SPA talks to the kdf daemon directly)",
        root_canonical.display(),
        addr
    );
    print_auto_config_banner(&auto_config);
    print_fx_banner(&fx_rates);

    let make_svc = make_service_fn(|_conn| {
        let root = root_canonical.clone();
        let auto_config = auto_config.clone();
        let fx_rates = fx_rates.clone();
        let proxy = proxy.clone();
        let rpc = rpc.clone();
        async move {
            Ok::<_, Infallible>(service_fn(move |req: Request<Body>| {
                let root = root.clone();
                let auto_config = auto_config.clone();
                let fx_rates = fx_rates.clone();
                let proxy = proxy.clone();
                let rpc = rpc.clone();
                async move { handle(req, root, auto_config, fx_rates, proxy, rpc).await }
            }))
        }
    });

    Server::try_bind(&addr)
        .unwrap_or_else(|e| panic!("elek-web cannot bind {}: {}", addr, e))
        .serve(make_svc)
        .await
        .unwrap_or_else(|e| panic!("elek-web server error: {}", e));
}

fn print_auto_config_banner(auto_config: &Option<(String, String)>) {
    match auto_config {
        Some((rpc_url, _)) => println!(
            "elek-web: auto-connect enabled -> {} handoff, daemon {}, connect form off",
            CONFIG_PATH, rpc_url
        ),
        None => println!(
            "elek-web: no MM_WEB_RPC_URL/MM_WEB_RPC_PASS pair -> {} is a 404, the SPA shows the connect form",
            CONFIG_PATH
        ),
    }
}

fn print_fx_banner(fx_rates: &Option<PathBuf>) {
    match fx_rates {
        Some(path) => println!(
            "elek-web: serving electrs FX rates file {} at {} (rate line in the SPA)",
            path.display(),
            FX_RATES_PATH
        ),
        None => println!(
            "elek-web: no MM_WEB_FX_RATES -> {} is a 404 (no rate line in the SPA)",
            FX_RATES_PATH
        ),
    }
}