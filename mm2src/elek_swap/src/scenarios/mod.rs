mod init_coins;
mod init_mm2_cfg;
mod inquire_extentions;
mod mm2_proc_mng;

use anyhow::Result;
use init_coins::init_coins;
use init_mm2_cfg::init_mm2_cfg;
use log::info;

use super::activation_scheme_db::init_activation_scheme;

pub(super) use mm2_proc_mng::{get_status, start_process, stop_process};

pub(super) async fn init(cfg_file: &str, coins_file: &str) { let _ = init_impl(cfg_file, coins_file).await; }

async fn init_impl(cfg_file: &str, coins_file: &str) -> Result<()> {
    init_mm2_cfg(cfg_file)?;
    init_coins(coins_file).await?;
    // the scheme is derived from the coins file the daemon will actually
    // run on, so it has to be generated after init_coins
    init_activation_scheme(coins_file)?;
    info!("Initialization done");
    Ok(())
}
