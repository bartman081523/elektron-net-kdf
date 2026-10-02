// error_anyhow! expands textually at the call site, so the caller-side
// module has to import the underlying `anyhow!` and `error!` macros itself
use anyhow::{anyhow, Result};
use common::log::{error, info};
use serde_json::{json, Value as Json};
use std::path::{Path, PathBuf};

use crate::adex_config::AdexConfigImpl;
use crate::helpers::{read_json_file, rewrite_json_file};
use crate::logging::error_anyhow;

const ACTIVATION_SCHEME_FILE: &str = "activation_scheme.json";

// Upstream adex_cli downloads the activation scheme from stats.kmd.io; that
// service knows nothing about the elektron coins, and the market has to work
// without a third-party dependency anyway. The scheme is therefore derived
// locally from the coins file the daemon is going to run on: every coin
// carrying an "electrum" server list is activated through those servers.
pub(crate) fn init_activation_scheme(coins_file: &str) -> Result<()> {
    let coins: Vec<Json> = read_json_file(Path::new(coins_file))?;
    let activation_scheme = scheme_from_coins(&coins);

    let config_path = get_activation_scheme_path()?;
    let config_path_str = config_path
        .to_str()
        .ok_or_else(|| error_anyhow!("Failed to get activation_scheme path as str: {config_path:?}"))?;
    rewrite_json_file(&activation_scheme, config_path_str)?;
    info!("Activation scheme written into: {config_path:?}");
    Ok(())
}

fn scheme_from_coins(coins: &[Json]) -> Json {
    let results: Vec<Json> = coins
        .iter()
        .filter_map(|coin| {
            let ticker = coin.get("coin")?.as_str()?;
            let servers: Vec<Json> = coin
                .get("electrum")?
                .as_array()?
                .iter()
                .filter_map(|server| match server {
                    // the coins file may list plain "host:port" strings
                    Json::String(url) => Some(json!({"url": url})),
                    server @ Json::Object(_) => Some(server.clone()),
                    _ => None,
                })
                .collect();
            if servers.is_empty() {
                return None;
            }
            Some(json!({"coin": ticker, "command": {"method": "electrum", "coin": ticker, "servers": servers}}))
        })
        .collect();
    json!({"results": results})
}

pub(crate) fn get_activation_scheme_path() -> Result<PathBuf> {
    let mut config_path = AdexConfigImpl::get_config_dir()?;
    config_path.push(ACTIVATION_SCHEME_FILE);
    Ok(config_path)
}