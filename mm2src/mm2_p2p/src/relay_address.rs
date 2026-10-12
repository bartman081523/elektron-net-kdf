use crate::{NetworkInfo, NetworkPorts};
use derive_more::Display;
use lazy_static::lazy_static;
use libp2p::Multiaddr;
use serde::{de, Deserialize, Deserializer, Serialize};
use std::str::FromStr;

#[derive(Clone, Debug, Display, Serialize)]
pub enum RelayAddressError {
    #[display(
        fmt = "Error parsing 'RelayAddress' from {found}: address has unknown protocol, expected either IPv4 or DNS or Memory or WebSocket address"
    )]
    FromStrError { found: String },
    #[display(fmt = "Error converting '{self_str:?}' to Multiaddr: unexpected IPv4/DNS address on a memory network")]
    DistributedAddrOnMemoryNetwork { self_str: String },
    #[display(
        fmt = "Error converting '{self_str:?}' to Multiaddr: unexpected memory address on a distributed network"
    )]
    MemoryAddrOnDistributedNetwork { self_str: String },
}

impl std::error::Error for RelayAddressError {}

impl RelayAddressError {
    fn distributed_addr_on_memory_network(addr: &RelayAddress) -> RelayAddressError {
        RelayAddressError::DistributedAddrOnMemoryNetwork {
            self_str: format!("{addr:?}"),
        }
    }

    fn memory_addr_on_distributed_network(addr: &RelayAddress) -> RelayAddressError {
        RelayAddressError::MemoryAddrOnDistributedNetwork {
            self_str: format!("{addr:?}"),
        }
    }
}

/// A `ws://`/`wss://` seednode: an explicit host and dial port. Unlike the
/// bare IPv4/DNS forms, whose ports are derived from the node's netid, a
/// websocket seed node carries the port in the string itself. Our render.com
/// federation dials the seed as `wss://elektron-market.onrender.com:443` —
/// the only externally reachable route there is HTTPS (render terminates TLS
/// and elek-web splices the upgrade onto the relay's plain-ws port), so the
/// bare seed forms (netid-derived ports) could never reach it.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct WsRelayAddress {
    /// IPv4 literal or a DNS name.
    pub host: String,
    /// Explicit dial port. Defaults: 443 for wss, 80 for ws.
    pub port: u16,
    /// `wss` (TLS) when true, plain `ws` when false.
    pub secure: bool,
}

impl std::fmt::Display for WsRelayAddress {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        let scheme = if self.secure { "wss" } else { "ws" };
        // URI-standard bracket form for IPv6 literals (stored un-bracketed,
        // see `from_host_port`): `ws://[::1]:10200`.
        if std::net::Ipv6Addr::from_str(&self.host).is_ok() {
            write!(f, "{scheme}://[{}]:{}", self.host, self.port)
        } else {
            write!(f, "{scheme}://{}:{}", self.host, self.port)
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum RelayAddress {
    IPv4(String),
    Dns(String),
    Memory(u64),
    WebSocket(WsRelayAddress),
}

impl std::fmt::Display for RelayAddress {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        match self {
            RelayAddress::IPv4(ip) => write!(f, "{}", ip),
            RelayAddress::Dns(dns) => write!(f, "{}", dns),
            RelayAddress::Memory(port) => write!(f, "/memory/{}", port),
            RelayAddress::WebSocket(addr) => write!(f, "{}", addr),
        }
    }
}

impl FromStr for RelayAddress {
    type Err = RelayAddressError;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        // check if the string is a websocket seed node: "wss://host[:port]"
        // or "ws://host[:port]"
        for (prefix, secure) in [("wss://", true), ("ws://", false)] {
            if let Some(rest) = s.strip_prefix(prefix) {
                return WsRelayAddress::from_host_port(rest, secure)
                    .map(RelayAddress::WebSocket)
                    .map_err(|_| RelayAddressError::FromStrError { found: s.to_owned() });
            }
        }
        // check if the string is IPv4
        if std::net::Ipv4Addr::from_str(s).is_ok() {
            return Ok(RelayAddress::IPv4(s.to_string()));
        }
        // check if the string is a domain name
        if validate_domain_name(s) {
            return Ok(RelayAddress::Dns(s.to_owned()));
        }
        // check if the string is a `/memory/<PORT>` address
        if let Some(port_str) = s.strip_prefix("/memory/") {
            if let Ok(port) = port_str.parse() {
                return Ok(RelayAddress::Memory(port));
            }
        }
        Err(RelayAddressError::FromStrError { found: s.to_owned() })
    }
}

impl WsRelayAddress {
    fn from_host_port(s: &str, secure: bool) -> Result<Self, ()> {
        // A bracketed IPv6 literal carries ':' inside the address itself, so
        // it must be split out BEFORE any "host:port" splitting. The stored
        // host is WITHOUT brackets (`Display` and `ws_multiaddr` re-derive
        // them, so parsing round-trips).
        if let Some(after_open) = s.strip_prefix('[') {
            let (inner, rest) = match after_open.split_once(']') {
                Some((inner, rest)) => (inner, rest),
                None => return Err(()),
            };
            let port: u16 = match rest.strip_prefix(':') {
                Some(port_str) => port_str.parse().map_err(|_| ())?,
                None if rest.is_empty() => if secure { 443 } else { 80 },
                None => return Err(()),
            };
            if port == 0 {
                return Err(());
            }
            let inner = inner.to_ascii_lowercase();
            if std::net::Ipv6Addr::from_str(&inner).is_err() {
                return Err(());
            }
            return Ok(WsRelayAddress {
                host: inner,
                port,
                secure,
            });
        }
        // Unbracketed form: split at the last ':'; the host must stay an
        // IPv4 literal or a DNS name. Bare IPv6 is rejected throughout — a
        // port-less dual-colon literal ("::1") is unsavable here (the ":" +
        // digits would be read as a port) and even a colon-suffixed one
        // ("2001:db8::1:8080") is ambiguous between address+port and a
        // longer address, per RFC 3986 the bracket form is the only way to
        // write an IPv6 websocket seed: "ws://[2001:db8::1]:8080".
        let (host, port) = match s.rsplit_once(':') {
            Some((host, port)) => {
                let port: u16 = port.parse().map_err(|_| ())?;
                (host, port)
            }
            None => (s, if secure { 443 } else { 80 }),
        };
        if port == 0 {
            return Err(());
        }
        // hosts are IPv4 literals or DNS names, case-insensitive: normalize
        // to lowercase
        let host = host.to_ascii_lowercase();
        if host.is_empty() {
            return Err(());
        }
        if std::net::Ipv4Addr::from_str(&host).is_err() && !validate_domain_name(&host) {
            return Err(());
        }
        Ok(WsRelayAddress { host, port, secure })
    }
}

impl<'de> Deserialize<'de> for RelayAddress {
    fn deserialize<D>(deserializer: D) -> Result<Self, <D as Deserializer<'de>>::Error>
    where
        D: Deserializer<'de>,
    {
        let addr_str = String::deserialize(deserializer)?;
        RelayAddress::from_str(&addr_str).map_err(de::Error::custom)
    }
}

impl RelayAddress {
    /// Try to convert `RelayAddress` to `Multiaddr` using the given `network_info`.
    pub fn try_to_multiaddr(&self, network_info: NetworkInfo) -> Result<Multiaddr, RelayAddressError> {
        let network_ports = match network_info {
            NetworkInfo::InMemory => match self {
                RelayAddress::Memory(port) => return Ok(memory_multiaddr(*port)),
                _ => return Err(RelayAddressError::distributed_addr_on_memory_network(self)),
            },
            NetworkInfo::Distributed { network_ports } => network_ports,
        };

        match self {
            RelayAddress::IPv4(ipv4) => Ok(ipv4_multiaddr(ipv4, network_ports)),
            RelayAddress::Dns(dns) => Ok(dns_multiaddr(dns, network_ports)),
            RelayAddress::Memory(_) => Err(RelayAddressError::memory_addr_on_distributed_network(self)),
            RelayAddress::WebSocket(addr) => Ok(ws_multiaddr(addr)),
        }
    }
}

/// Use [this](https://regex101.com/r/94nCB5/1) regular expression to validate the domain name.
/// See examples at the linked resource above.
fn validate_domain_name(s: &str) -> bool {
    use regex::Regex;

    lazy_static! {
        static ref DNS_REGEX: Regex = Regex::new(r#"^([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,}$"#).unwrap();
    }

    DNS_REGEX.is_match(s)
}

fn memory_multiaddr(port: u64) -> Multiaddr {
    format!("/memory/{port}").parse().unwrap()
}

#[cfg(target_arch = "wasm32")]
fn ipv4_multiaddr(ipv4_addr: &str, ports: NetworkPorts) -> Multiaddr {
    format!("/ip4/{}/tcp/{}/wss", ipv4_addr, ports.wss).parse().unwrap()
}

#[cfg(not(target_arch = "wasm32"))]
fn ipv4_multiaddr(ipv4_addr: &str, ports: NetworkPorts) -> Multiaddr {
    format!("/ip4/{}/tcp/{}", ipv4_addr, ports.tcp).parse().unwrap()
}

#[cfg(target_arch = "wasm32")]
fn dns_multiaddr(dns_addr: &str, ports: NetworkPorts) -> Multiaddr {
    format!("/dns/{}/tcp/{}/wss", dns_addr, ports.wss).parse().unwrap()
}

/// A websocket seed node dials its explicit host+port; nothing is derived
/// from the netid here. The native transport already carries the websocket
/// branch (`dns_tcp.or_transport(ws_dns_tcp)` in behaviours/atomicdex.rs), so
/// `/ws` dials (through a splicing proxy) and `/wss` dials (TLS; the client
/// side trusts the system-wide webpki roots by default, see
/// transports/websocket/src/tls.rs in the libp2p fork) both work.
fn ws_multiaddr(addr: &WsRelayAddress) -> Multiaddr {
    // `Protocol::Dns` is the DUAL-STACK resolver branch (transports/dns in
    // the libp2p fork: `resolver.lookup_ip` collects A *and* AAAA records
    // and the dial tries each resolved candidate until one succeeds), so a
    // DNS seed node dials from IPv4-only and IPv6-only machines alike.
    // `/dns4/` (A records only) would strand IPv6-only peers. IPv6 literals
    // map to `/ip6/`; `Protocol::Dns6`-style AAAA-only dialing stays out of
    // scope here.
    let proto = if std::net::Ipv4Addr::from_str(&addr.host).is_ok() {
        "ip4"
    } else if std::net::Ipv6Addr::from_str(&addr.host).is_ok() {
        "ip6"
    } else {
        "dns"
    };
    let suffix = if addr.secure { "wss" } else { "ws" };
    format!("/{}/{}/tcp/{}/{}", proto, addr.host, addr.port, suffix)
        .parse()
        .unwrap()
}

#[cfg(not(target_arch = "wasm32"))]
fn dns_multiaddr(dns_addr: &str, ports: NetworkPorts) -> Multiaddr {
    format!("/dns/{}/tcp/{}", dns_addr, ports.tcp).parse().unwrap()
}

#[test]
fn test_relay_address_from_str() {
    let valid_addresses = vec![
        ("127.0.0.1", RelayAddress::IPv4("127.0.0.1".to_owned())),
        ("255.255.255.255", RelayAddress::IPv4("255.255.255.255".to_owned())),
        ("google.com", RelayAddress::Dns("google.com".to_owned())),
        ("www.google.com", RelayAddress::Dns("www.google.com".to_owned())),
        ("g.co", RelayAddress::Dns("g.co".to_owned())),
        (
            "stackoverflow.co.uk",
            RelayAddress::Dns("stackoverflow.co.uk".to_owned()),
        ),
        ("1.2.3.4.com", RelayAddress::Dns("1.2.3.4.com".to_owned())),
        ("/memory/123", RelayAddress::Memory(123)),
        ("/memory/71428421981", RelayAddress::Memory(71428421981)),
        (
            "wss://elektron-market.onrender.com:443",
            RelayAddress::WebSocket(WsRelayAddress {
                host: "elektron-market.onrender.com".to_owned(),
                port: 443,
                secure: true,
            }),
        ),
        (
            "ws://127.0.0.1:10100",
            RelayAddress::WebSocket(WsRelayAddress {
                host: "127.0.0.1".to_owned(),
                port: 10100,
                secure: false,
            }),
        ),
        (
            "wss://Render.com",
            RelayAddress::WebSocket(WsRelayAddress {
                host: "render.com".to_owned(),
                port: 443,
                secure: true,
            }),
        ),
        (
            "ws://g.co",
            RelayAddress::WebSocket(WsRelayAddress {
                host: "g.co".to_owned(),
                port: 80,
                secure: false,
            }),
        ),
    ];
    for (s, expected) in valid_addresses {
        let actual = RelayAddress::from_str(s).unwrap_or_else(|_| panic!("Error parsing '{s}'"));
        assert_eq!(actual, expected);
    }

    let invalid_addresses = vec![
        "127.0.0",
        "127.0.0.0.2",
        "google.c",
        "http://google.com",
        "https://google.com/",
        "google.com/",
        "/memory/",
        "/memory/9999999999999999999999999999999",
        // websocket forms: missing or malformed host/port
        "wss://",
        "ws://",
        "wss://google.com:",
        "wss://google.com:bad",
        "ws://google.com:0",
        "ws://google",
        "wss://google.c:443",
        "ws://google.com/:443",
        // bracket form requires an IPv6 literal inside
        "ws://[google.com]:443",
        "ws://[::1",
        "ws://[bad-v6]:443",
        // a port-less bare literal is indistinguishable from a fragment
        "ws://::1",
        // bare literal with a JUNK trailing group
        "ws://::1:99999",
    ];
    for s in invalid_addresses {
        let _ = RelayAddress::from_str(s).expect_err("Expected an error");
    }
}

#[test]
fn test_ws_relay_address_to_multiaddr() {
    use crate::NetworkInfo;

    let ports = NetworkPorts { tcp: 43331, wss: 43341 };
    let distributed = NetworkInfo::Distributed {
        network_ports: ports.clone(),
    };

    // `elektron-market.onrender.com:443`: our render.com seed form. DNS
    // names resolve DUAL-STACK (`/dns/`, A+AAAA) so IPv6-only peers can
    // dial the federation edge.
    let seed = RelayAddress::from_str("wss://elektron-market.onrender.com:443").unwrap();
    assert_eq!(seed.try_to_multiaddr(distributed.clone()).unwrap().to_string(),
               "/dns/elektron-market.onrender.com/tcp/443/wss");

    // a local bridge target rides the same mechanics without TLS
    let seed = RelayAddress::from_str("ws://127.0.0.1:10100").unwrap();
    assert_eq!(seed.try_to_multiaddr(distributed.clone()).unwrap().to_string(),
               "/ip4/127.0.0.1/tcp/10100/ws");

    // IPv6 literals: the URI-standard bracket form dials `/ip6/`, with and
    // without an explicit port. Bare dual-colon literals stay rejected (see
    // `from_host_port` — the bracket form is the only unambiguous spelling).
    let seed = RelayAddress::from_str("ws://[::1]:10200").unwrap();
    assert_eq!(seed.try_to_multiaddr(distributed.clone()).unwrap().to_string(),
               "/ip6/::1/tcp/10200/ws");
    assert_eq!(seed.to_string(), "ws://[::1]:10200");

    let seed = RelayAddress::from_str("ws://[::1]").unwrap();
    assert_eq!(seed.try_to_multiaddr(distributed.clone()).unwrap().to_string(),
               "/ip6/::1/tcp/80/ws");
    assert_eq!(seed.to_string(), "ws://[::1]:80");

    for bare in ["ws://::1:8080", "ws://2001:db8::1", "ws://2001:db8::1:2"] {
        assert!(RelayAddress::from_str(bare).is_err(), "expected bare '{bare}' rejected");
    }

    // netid-derived ports must NOT leak into the websocket form
    let seed = RelayAddress::from_str("ws://relay.host:10200").unwrap();
    assert_eq!(seed.try_to_multiaddr(distributed).unwrap().to_string(),
               "/dns/relay.host/tcp/10200/ws");

    let seed = RelayAddress::from_str("ws://relay.host:10200").unwrap();
    assert!(seed.try_to_multiaddr(NetworkInfo::InMemory).is_err());
}

#[test]
fn test_deserialize_relay_address() {
    #[derive(Deserialize, PartialEq)]
    struct Config {
        addresses: Vec<RelayAddress>,
    }

    let Config { addresses: actual } =
        common::serde_json::from_str(r#"{"addresses": ["foo.bar.com", "127.0.0.2", "/memory/12345"]}"#)
            .expect("Error deserializing a list of RelayAddress");
    let expected = vec![
        RelayAddress::Dns("foo.bar.com".to_owned()),
        RelayAddress::IPv4("127.0.0.2".to_owned()),
        RelayAddress::Memory(12345),
    ];
    assert_eq!(actual, expected);
}

#[test]
fn test_deserialize_ws_relay_address() {
    #[derive(Deserialize, PartialEq)]
    struct Config {
        addresses: Vec<RelayAddress>,
    }

    let Config { addresses: actual } = common::serde_json::from_str(
        r#"{"addresses": ["wss://elektron-market.onrender.com:443", "ws://127.0.0.1:10100", "ws://google.com"]}"#,
    )
    .expect("Error deserializing a list of RelayAddress");
    let expected = vec![
        RelayAddress::WebSocket(WsRelayAddress {
            host: "elektron-market.onrender.com".to_owned(),
            port: 443,
            secure: true,
        }),
        RelayAddress::WebSocket(WsRelayAddress {
            host: "127.0.0.1".to_owned(),
            port: 10100,
            secure: false,
        }),
        RelayAddress::WebSocket(WsRelayAddress {
            host: "google.com".to_owned(),
            port: 80,
            secure: false,
        }),
    ];
    assert_eq!(actual, expected);
}
