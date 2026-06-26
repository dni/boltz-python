#!/bin/sh
export COMPOSE_PROJECT_NAME=boltz

if [[ $(docker --help | grep compose) ]]; then
  export COMPOSE_CMD="docker compose"
else
  export COMPOSE_CMD="docker-compose"
fi

bitcoin-cli-sim() {
  docker exec boltz-bitcoind bitcoin-cli -rpcuser=boltz -rpcpassword=boltz -regtest "$@"
}

lightning-cli-sim() {
  docker exec boltz-cln-1 lightning-cli --network regtest --lightning-dir=/app/lightning "$@"
}

elements-cli-sim() {
  docker exec boltz-elementsd elements-cli -rpcuser=boltz -rpcpassword=boltz "$@"
}

lncli-sim() {
  docker exec boltz-lnd-1 lncli --network regtest --lnddir=/app/lnd "$@"
}

fund_cln_node() {
  address=$(lightning-cli-sim newaddr | jq -r .p2tr)
  echo "funding: $address on cln-1 node."
  bitcoin-cli-sim -named sendtoaddress address="$address" amount=30 fee_rate=1 > /dev/null
}

fund_lnd_node() {
  address=$(lncli-sim newaddress p2wkh | jq -r .address)
  echo "funding: $address on lnd-1 node."
  bitcoin-cli-sim -named sendtoaddress address="$address" amount=30 fee_rate=1 > /dev/null
}

wait-for-boltz(){
  echo "waiting for boltz backend..."
  while true; do
    if curl -sf http://localhost:9001/version > /dev/null 2>&1; then
      echo "boltz backend is ready!"
      break
    fi
    sleep 2
  done
}

regtest-start(){
  regtest-stop
  $COMPOSE_CMD up -d --remove-orphans
  regtest-init
  wait-for-boltz
}

regtest-stop(){
  $COMPOSE_CMD down --volumes
}

regtest-restart(){
  regtest-stop
  regtest-start
}

bitcoin-init(){
  echo "init_bitcoin_wallet..."
  bitcoin-cli-sim createwallet regtest 2>/dev/null || bitcoin-cli-sim loadwallet regtest 2>/dev/null || true
  echo "mining 150 blocks..."
  bitcoin-cli-sim -rpcwallet=regtest -generate 150 > /dev/null
}

elements-init(){
  echo "init_elements_wallet..."
  elements-cli-sim createwallet regtest 2>/dev/null || elements-cli-sim loadwallet regtest true 2>/dev/null || true
  echo "mining 150 liquid blocks..."
  elements-cli-sim -rpcwallet=regtest -generate 150 > /dev/null
  elements-cli-sim rescanblockchain 0 > /dev/null
  echo "elements rescan blockchain..."
}

regtest-init(){
  bitcoin-init
  elements-init
  lightning-sync
  lightning-init
}

lightning-sync(){
  wait-for-cln-sync
  wait-for-lnd-sync
}

lightning-init(){
  # create 5 UTXOs for each node
  for i in 0 1 2 3 4; do
    fund_cln_node
    fund_lnd_node
  done

  echo "mining 10 blocks..."
  bitcoin-cli-sim -rpcwallet=regtest -generate 10 > /dev/null

  echo "waiting 5s for nodes to catch up..."
  sleep 5

  lightning-sync

  channel_size=24000000
  balance_size=12000000

  # lnd-1 (boltz server) -> cln-1 (test client)
  cln_pubkey=$(lightning-cli-sim getinfo | jq -r '.id')
  lncli-sim connect "${cln_pubkey}@cln-1:9735" > /dev/null
  echo "open channel from lnd-1 to cln-1"
  lncli-sim openchannel "$cln_pubkey" $channel_size $balance_size > /dev/null
  bitcoin-cli-sim -rpcwallet=regtest -generate 10 > /dev/null
  wait-for-lnd-channel
  lightning-sync
}

wait-for-lnd-channel(){
  while true; do
    pending=$(lncli-sim pendingchannels | jq -r '.pending_open_channels | length')
    echo "lnd-1 pendingchannels: $pending"
    if [[ "$pending" == "0" ]]; then
      break
    fi
    sleep 1
  done
}

wait-for-lnd-sync(){
  while true; do
    if [[ "$(lncli-sim getinfo 2>&1 | jq -r '.synced_to_chain' 2> /dev/null)" == "true" ]]; then
      echo "lnd-1 is synced!"
      break
    fi
    echo "waiting for lnd-1 to sync..."
    sleep 1
  done
}

wait-for-cln-sync(){
  while true; do
    if [[ ! "$(lightning-cli-sim getinfo 2>&1 | jq -r '.id' 2> /dev/null)" == "null" ]]; then
      if [[ "$(lightning-cli-sim getinfo 2>&1 | jq -r '.warning_bitcoind_sync' 2> /dev/null)" == "null" ]]; then
        if [[ "$(lightning-cli-sim getinfo 2>&1 | jq -r '.warning_lightningd_sync' 2> /dev/null)" == "null" ]]; then
          echo "cln-1 is synced!"
          break
        fi
      fi
    fi
    echo "waiting for cln-1 to sync..."
    sleep 1
  done
}

wait-for-cln-channel(){
  while true; do
    pending=$(lightning-cli-sim getinfo | jq -r '.num_pending_channels | length')
    echo "cln-1 pendingchannels: $pending"
    if [[ "$pending" == "0" ]]; then
      if [[ "$(lightning-cli-sim getinfo 2>&1 | jq -r '.warning_bitcoind_sync' 2> /dev/null)" == "null" ]]; then
        if [[ "$(lightning-cli-sim getinfo 2>&1 | jq -r '.warning_lightningd_sync' 2> /dev/null)" == "null" ]]; then
          break
        fi
      fi
    fi
    sleep 1
  done
}
