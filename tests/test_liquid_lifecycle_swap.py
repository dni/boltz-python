import asyncio

import pytest

from boltz_client.boltz import (
    BoltzClient,
    BoltzSwapStatusException,
)

from .helpers import create_onchain_address, mine_blocks, pay_onchain


@pytest.mark.asyncio
async def test_create_swap_and_check_status(client_liquid: BoltzClient, liquid_pr):
    _, swap = client_liquid.create_swap(liquid_pr)

    _ = pay_onchain(swap.address, swap.expectedAmount, client_liquid.pair)

    await asyncio.sleep(1)

    swap_status_after_payment = client_liquid.swap_status(swap.id)
    assert swap_status_after_payment.status == "transaction.mempool"

    mine_blocks(client_liquid.pair)

    for _ in range(30):
        swap_status_after_confirmed = client_liquid.swap_status(swap.id)
        if swap_status_after_confirmed.status == "transaction.claimed":
            break
        if swap_status_after_confirmed.status == "transaction.claim.pending":
            mine_blocks(client_liquid.pair)
        await asyncio.sleep(1)
    assert swap_status_after_confirmed.status in [
        "transaction.claim.pending",
        "transaction.claimed",
    ]


@pytest.mark.asyncio
async def test_create_swap_and_refund(client_liquid: BoltzClient, liquid_pr_refund):
    refund_privkey_wif, swap = client_liquid.create_swap(liquid_pr_refund)

    # pay to less onchain so the swap fails
    _ = pay_onchain(swap.address, swap.expectedAmount - 1000, client_liquid.pair)

    await asyncio.sleep(1)
    mine_blocks(client_liquid.pair)
    await asyncio.sleep(1)

    with pytest.raises(BoltzSwapStatusException):
        client_liquid.swap_status(swap.id)

    onchain_address = create_onchain_address(client_liquid.pair)

    # wait for timeout
    mine_blocks(pair=client_liquid.pair, blocks=1000)

    await asyncio.sleep(10)

    # actually refund
    pytest.xfail("Liquid v2 taproot refund transaction is not implemented")
    _ = await client_liquid.refund_swap(
        boltz_id=swap.id,
        privkey_wif=refund_privkey_wif,
        lockup_address=swap.address,
        receive_address=onchain_address,
        swap_tree=swap.swapTree,
        boltz_pubkey=swap.claimPublicKey,
        timeout_block_height=swap.timeoutBlockHeight,
        blinding_key=swap.blindingKey,
    )

    mine_blocks(pair=client_liquid.pair)
    await asyncio.sleep(1)

    # check status
    try:
        client_liquid.swap_status(swap.id)
    except BoltzSwapStatusException as exc:
        assert exc.status == "swap.expired"
