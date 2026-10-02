//! elek-web: serves the market web UI (`web/` at the repo root) on loopback so
//! the browser can talk to the local kdf daemon directly (CORS handled by the
//! daemon's `rpccors`; see doc/elektron.md section 11).
//!
//! Deliberately dependency-minimal (hyper + tokio, nothing else): static
//! files come from disk (no embedding crates in the workspace), the SPA uses
//! hash routing so only real files exist under MM_WEB_ROOT.

use hyper::{
    header::{
        self, HeaderValue, ALLOW, CACHE_CONTROL, CONTENT_LENGTH, CONTENT_TYPE,
    },
    service::{make_service_fn, service_fn},
    Body, Method, Request, Response, Server, StatusCode,
};
use std::{
    convert::Infallible,
    env,
    net::SocketAddr,
    path::{Path, PathBuf},
};

const DEFAULT_ROOT: &str = "web";
const DEFAULT_ADDR: &str = "127.0.0.1:3000";

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

async fn handle(req: Request<Body>, root: PathBuf) -> Result<Response<Body>, Infallible> {
    let is_head = matches!(*req.method(), Method::HEAD);
    let method = req.method().clone();
    let path = req.uri().path().to_string();
    let mut resp = match *req.method() {
        Method::GET | Method::HEAD => match resolve(&root, &path) {
            Some(file) => send_file(file, is_head).await,
            None => not_found(),
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

    println!(
        "elek-web: serving {} on http://{} (SPA talks to the kdf daemon directly)",
        root_canonical.display(),
        addr
    );

    let make_svc = make_service_fn(|_conn| {
        let root = root_canonical.clone();
        async move {
            Ok::<_, Infallible>(service_fn(move |req: Request<Body>| {
                let root = root.clone();
                async move { handle(req, root).await }
            }))
        }
    });

    Server::try_bind(&addr)
        .unwrap_or_else(|e| panic!("elek-web cannot bind {}: {}", addr, e))
        .serve(make_svc)
        .await
        .unwrap_or_else(|e| panic!("elek-web server error: {}", e));
}