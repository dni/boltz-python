"""boltz_client test helpers"""
import json
import time
from typing import Optional
from subprocess import PIPE, Popen, run

docker_cmd = "docker exec"

docker_lightning = "boltz-cln-1"
docker_lightning_cli = "lightning-cli --network regtest --lightning-dir=/app/lightning"

docker_bitcoin = "boltz-bitcoind"
docker_bitcoin_cli = "bitcoin-cli -rpcuser=boltz -rpcpassword=boltz -regtest"

docker_elements = "boltz-elementsd"
docker_elements_cli = "elements-cli -rpcuser=boltz -rpcpassword=boltz"


def run_cmd(cmd: str) -> str:
    return run(cmd, shell=True, capture_output=True).stdout.decode("UTF-8").strip()


def get_docker_cmd(container: str, cmd: str) -> str:
    return f"{docker_cmd} {container} {cmd}"


def get_invoice(sats: int, prefix: str, description: str = "test") -> dict:
    msats = sats * 1000
    cli_cmd = f"invoice {msats} {prefix}-{time.time()} {description}"
    cmd = get_docker_cmd(docker_lightning, f"{docker_lightning_cli} {cli_cmd}")
    return json.loads(run_cmd(cmd))


def pay_invoice(invoice: str) -> Popen:
    cli_cmd = f"pay {invoice}"
    cmd = get_docker_cmd(docker_lightning, f"{docker_lightning_cli} {cli_cmd}")
    return Popen(cmd, shell=True, stdin=PIPE, stdout=PIPE)


def run_core_cli_cmd(pair: str, cli_cmd: str) -> str:
    if pair == "L-BTC/BTC":
        cmd = get_docker_cmd(docker_elements, f"{docker_elements_cli} {cli_cmd}")
    else:
        cmd = get_docker_cmd(docker_bitcoin, f"{docker_bitcoin_cli} {cli_cmd}")
    return run_cmd(cmd)


def mine_blocks(pair: str = "BTC/BTC", blocks: int = 1) -> str:
    return run_core_cli_cmd(pair, f"-rpcwallet=regtest -generate {blocks}")


def create_onchain_address(pair: str = "BTC/BTC", address_type: str = "bech32") -> str:
    return run_core_cli_cmd(pair, f"getnewaddress {address_type}")


def pay_onchain(address: str, sats: int, pair: str = "BTC/BTC") -> str:
    btc = sats / 10**8
    return run_core_cli_cmd(pair, f"-rpcwallet=regtest sendtoaddress {address} {btc}")
