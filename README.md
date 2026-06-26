# Boltz Python Client
Boltz Client in Python, implementing mainchain and liquid submarine swaps. Used by e.g. https://github.com/lnbits/boltz.

Supports the Boltz v2 API with taproot-based swaps (MuSig2 key aggregation).

# CLI
```console
$ boltz --help
Usage: boltz [OPTIONS] COMMAND [ARGS]...

  Python CLI of boltz-client-python, enjoy submarine swapping. :)

Options:
  --help  Show this message and exit.

Commands:
  calculate-swap-send-amount     Calculate the invoice amount needed to...
  claim-reverse-swap             Claim a reverse swap output.
  create-reverse-swap            Create a reverse swap (Lightning → on-chain).
  create-reverse-swap-and-claim  Create a reverse swap and automatically claim.
  create-swap                    Create a submarine swap (on-chain → Lightning).
  refund-swap                    Refund a failed submarine swap.
  show-pairs                     Show available swap pairs.
  swap-status                    Get swap status.
```
install the latest release from [PyPI](https://pypi.org/project/boltz-client) via `pip install boltz_client`.

# LIB
### initialize the client
```python
from boltz_client import BoltzClient, BoltzConfig
config = BoltzConfig() # default config
client = BoltzClient(config, "BTC/BTC")
```
### lifecycle swap (submarine: on-chain → Lightning)
```python
pr = create_lightning_invoice(100000) # example function to create a lightning invoice
refund_privkey_wif, swap = client.create_swap(pr)
print(f"pay this amount: {swap.expectedAmount}")
print(f"to this address: {swap.address}")
# when you pay the amount the invoice will be settled after boltz claims the swap
```
if the swap fails you can refund like this:
```python
onchain_address = create_onchain_address() # example function to create an onchain address
txid = await client.refund_swap(
    boltz_id=swap.id,
    privkey_wif=refund_privkey_wif,
    lockup_address=swap.address,
    receive_address=onchain_address,
    swap_tree=swap.swapTree,
    boltz_pubkey=swap.claimPublicKey,
    timeout_block_height=swap.timeoutBlockHeight,
)
```

### lifecycle reverse swap (Lightning → on-chain)
```python
claim_privkey_wif, preimage_hex, swap = client.create_reverse_swap(50000)
pay_task = asyncio.create_task(pay_invoice(swap.invoice)) # example function to pay the invoice
new_address = create_onchain_address()                    # example function to create an onchain address
task = asyncio.create_task(client.claim_reverse_swap(
    boltz_id=swap.id,
    receive_address=new_address,
    lockup_address=swap.lockupAddress,
    swap_tree=swap.swapTree,
    boltz_pubkey=swap.refundPublicKey,
    privkey_wif=claim_privkey_wif,
    preimage_hex=preimage_hex,
    zeroconf=True,
))
txid = await task
await pay_task
```


# development

## installing
```console
poetry install
```

## running cli
```console
poetry run boltz
```

## starting regtest
```console
cd docker
docker compose up -d
./regtest-init
```

## running tests
```console
poetry run pytest
```
