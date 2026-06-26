"""boltz_client onchain module - Boltz v2 taproot"""

import os
from hashlib import sha256

import wallycore as wally
from embit import ec, script
from embit.base import EmbitError
from embit.hashes import tagged_hash, tagged_hash_init
from embit.liquid.networks import NETWORKS as LNETWORKS
from embit.networks import NETWORKS
from embit.script import Witness
from embit.transaction import SIGHASH, Transaction, TransactionInput, TransactionOutput
from embit.util import secp256k1

from .onchain_wally import NETWORKS as WALLY_NETWORKS
from .onchain_wally import decode_address

# secp256k1 curve order
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def validate_address(address: str, network: str, pair: str) -> str:
    if pair == "L-BTC/BTC":
        liquid_networks = {
            "liquidv1": "mainnet",
            "liquidtestnet": "testnet",
            "elementsregtest": "regtest",
        }
        wally_network_name = liquid_networks[network]
        wally_network = next(
            network for network in WALLY_NETWORKS if network.name == wally_network_name
        )
        try:
            decode_address(wally, wally_network, address)
        except Exception as exc:
            raise ValueError(f"Invalid address: {exc}") from exc
        return address
    else:
        net = NETWORKS[network]
        _address = address
    try:
        addr = script.Script.from_address(_address) or script.Script()
        if addr.address(net) != address:
            raise ValueError(f"Invalid network {network}")
        return address
    except EmbitError as exc:
        raise ValueError(f"Invalid address: {exc}") from exc


def create_preimage() -> tuple[str, str]:
    preimage = os.urandom(32)
    preimage_hash = sha256(preimage).hexdigest()
    return preimage.hex(), preimage_hash


def create_key_pair(network: str, pair: str) -> tuple[str, str]:
    if pair == "L-BTC/BTC":
        net = LNETWORKS[network]
    else:
        net = NETWORKS[network]
    privkey = ec.PrivateKey(os.urandom(32), True, net)
    pubkey_hex = bytes.hex(privkey.sec())
    privkey_wif = privkey.wif(net)
    return privkey_wif, pubkey_hex


def _tap_leaf_hash(script_hex: str, version: int = 0xC0) -> bytes:
    """BIP341 TapLeaf hash."""
    leaf_script = script.Script(data=bytes.fromhex(script_hex))
    return tagged_hash("TapLeaf", bytes([version]) + leaf_script.serialize())


def _tap_branch_hash(left: bytes, right: bytes) -> bytes:
    """BIP341 TapBranch hash. Sorts inputs so left <= right."""
    if right < left:
        left, right = right, left
    return tagged_hash("TapBranch", left + right)


def _musig2_key_agg(pk1_hex: str, pk2_hex: str) -> bytes:
    """BIP327 key aggregation for 2 pubkeys (33-byte compressed hex).
    pk1 gets the hash coefficient; pk2 (the 'second key') gets coefficient 1.
    Returns 32-byte x-only aggregate key.

    For Boltz taproot:
    - Submarine swap: pk1=boltz_claim_key, pk2=our_refund_key
    - Reverse swap:   pk1=boltz_refund_key, pk2=our_claim_key
    """
    pk1 = bytes.fromhex(pk1_hex)  # 33-byte compressed
    pk2 = bytes.fromhex(pk2_hex)

    # Use full 33-byte compressed keys for hashing (matches @scure/btc-signer)
    key_agg_hash = tagged_hash("KeyAgg list", pk1 + pk2)
    a1 = (
        int.from_bytes(tagged_hash("KeyAgg coefficient", key_agg_hash + pk1), "big")
        % _N
    )

    # Parse with actual y-parity (not forced even)
    point1 = secp256k1.ec_pubkey_parse(pk1)
    point2 = secp256k1.ec_pubkey_parse(pk2)

    secp256k1.ec_pubkey_tweak_mul(point1, a1.to_bytes(32, "big"))
    aggregate = secp256k1.ec_pubkey_combine(point1, point2)
    return secp256k1.ec_pubkey_serialize(aggregate, secp256k1.EC_COMPRESSED)[1:]


def _build_taproot(
    internal_xonly: bytes,
    claim_script_hex: str,
    refund_script_hex: str,
    leaf_version: int = 0xC0,
) -> tuple[bytes, bytes, bytes, bytes]:
    """Build a 2-leaf taproot tree (claim + refund leaves at depth 1).

    Returns output key, script pubkey, and both control blocks.
    """
    claim_h = _tap_leaf_hash(claim_script_hex, leaf_version)
    refund_h = _tap_leaf_hash(refund_script_hex, leaf_version)
    tree_hash = _tap_branch_hash(claim_h, refund_h)

    tweak = tagged_hash("TapTweak", internal_xonly + tree_hash)
    point = secp256k1.ec_pubkey_parse(b"\x02" + internal_xonly)
    out_point = secp256k1.ec_pubkey_add(point, tweak)
    out_compressed = secp256k1.ec_pubkey_serialize(out_point, secp256k1.EC_COMPRESSED)

    out_xonly = out_compressed[1:]
    parity = 0x01 if out_compressed[0] == 0x03 else 0x00

    p2tr_spk = bytes([0x51, 0x20]) + out_xonly
    cb_prefix = bytes([leaf_version | parity]) + internal_xonly
    claim_cb = cb_prefix + refund_h
    refund_cb = cb_prefix + claim_h

    return out_xonly, p2tr_spk, claim_cb, refund_cb


def _sighash_taproot_script_path(
    tx: Transaction,
    input_index: int,
    script_pubkeys: list[script.Script],
    values: list[int],
    script_hex: str,
    leaf_version: int = 0xC0,
    sighash: int = SIGHASH.DEFAULT,
) -> bytes:
    """BIP341 script-path taproot sighash for the default Boltz signing path."""
    sh, anyonecanpay = SIGHASH.check(sighash)
    if anyonecanpay or sh in [SIGHASH.SINGLE, SIGHASH.NONE]:
        raise ValueError("Unsupported taproot sighash type")

    h = tagged_hash_init("TapSighash", b"\x00")
    h.update(bytes([sighash]))
    h.update(tx.version.to_bytes(4, "little"))
    h.update(tx.locktime.to_bytes(4, "little"))
    h.update(tx.hash_prevouts())
    h.update(tx.hash_amounts(values))
    h.update(tx.hash_script_pubkeys(script_pubkeys))
    h.update(tx.hash_sequence())
    h.update(tx.hash_outputs())
    h.update(b"\x02")  # spend_type: ext_flag=1, annex_present=0
    h.update(input_index.to_bytes(4, "little"))
    h.update(_tap_leaf_hash(script_hex, leaf_version))
    h.update(b"\x00")  # key_version
    h.update((0xFFFFFFFF).to_bytes(4, "little"))  # code_separator_pos
    return h.digest()


def _find_utxo(lockup_rawtx: str, lockup_address: str) -> tuple[bytes, int, int]:
    """Parse lockup tx and find the output paying to lockup_address.
    Returns (txid_bytes, vout_index, vout_amount_sats).
    """
    try:
        lockup_tx = Transaction.from_string(lockup_rawtx)
    except EmbitError as exc:
        raise ValueError("Invalid lockup transaction hex") from exc

    lockup_spk = script.address_to_scriptpubkey(lockup_address)
    for i, vout in enumerate(lockup_tx.vout):
        if vout.script_pubkey == lockup_spk:
            return lockup_tx.txid(), i, vout.value

    raise ValueError("No matching vout found in lockup transaction for lockup_address")


def create_claim_tx(
    lockup_address: str,
    lockup_rawtx: str,
    receive_address: str,
    privkey_wif: str,
    preimage_hex: str,
    claim_script_hex: str,
    refund_script_hex: str,
    boltz_pubkey: str,
    fees: int,
    pair: str,
    leaf_version: int = 0xC0,
    blinding_key: str | None = None,
) -> str:
    """Build and sign a taproot script-path claim transaction (reverse swap)."""
    if pair == "L-BTC/BTC":
        raise NotImplementedError("Liquid taproot claim not yet implemented for v2")

    privkey = ec.PrivateKey.from_wif(privkey_wif)
    our_pubkey = privkey.sec().hex()

    # Reverse swap key order: [boltz_refund_key, our_claim_key]
    internal_xonly = _musig2_key_agg(boltz_pubkey, our_pubkey)
    _out_xonly, p2tr_spk, claim_cb, _ = _build_taproot(
        internal_xonly, claim_script_hex, refund_script_hex, leaf_version
    )

    expected_spk = script.address_to_scriptpubkey(lockup_address)
    if script.Script(data=p2tr_spk) != expected_spk:
        raise ValueError(
            "Computed taproot address does not match lockup_address; "
            "check key ordering or leaf scripts"
        )

    txid, vout_idx, vout_amount = _find_utxo(lockup_rawtx, lockup_address)

    vout = TransactionOutput(
        vout_amount - fees,
        script.address_to_scriptpubkey(receive_address),
    )
    vin = TransactionInput(txid, vout_idx, sequence=0xFFFFFFFF)
    tx = Transaction(vin=[vin], vout=[vout])

    sighash = _sighash_taproot_script_path(
        tx,
        input_index=0,
        script_pubkeys=[script.Script(data=p2tr_spk)],
        values=[vout_amount],
        script_hex=claim_script_hex,
        leaf_version=leaf_version,
    )
    sig = privkey.schnorr_sign(sighash)._sig

    tx.vin[0].witness = Witness(
        items=[
            sig,
            bytes.fromhex(preimage_hex),
            bytes.fromhex(claim_script_hex),
            claim_cb,
        ]
    )
    return bytes.hex(tx.serialize())


def create_refund_tx(
    lockup_address: str,
    lockup_rawtx: str,
    receive_address: str,
    privkey_wif: str,
    claim_script_hex: str,
    refund_script_hex: str,
    boltz_pubkey: str,
    timeout_block_height: int,
    fees: int,
    pair: str,
    leaf_version: int = 0xC0,
    blinding_key: str | None = None,
) -> str:
    """Build and sign a taproot script-path refund transaction (submarine swap)."""
    if pair == "L-BTC/BTC":
        raise NotImplementedError("Liquid taproot refund not yet implemented for v2")

    privkey = ec.PrivateKey.from_wif(privkey_wif)
    our_pubkey = privkey.sec().hex()

    # Submarine swap key order: [boltz_claim_key, our_refund_key]
    internal_xonly = _musig2_key_agg(boltz_pubkey, our_pubkey)
    _out_xonly, p2tr_spk, _, refund_cb = _build_taproot(
        internal_xonly, claim_script_hex, refund_script_hex, leaf_version
    )

    expected_spk = script.address_to_scriptpubkey(lockup_address)
    if script.Script(data=p2tr_spk) != expected_spk:
        raise ValueError(
            "Computed taproot address does not match lockup_address; "
            "check key ordering or leaf scripts"
        )

    txid, vout_idx, vout_amount = _find_utxo(lockup_rawtx, lockup_address)

    vout = TransactionOutput(
        vout_amount - fees,
        script.address_to_scriptpubkey(receive_address),
    )
    vin = TransactionInput(txid, vout_idx, sequence=0xFFFFFFFE)
    tx = Transaction(vin=[vin], vout=[vout])
    tx.locktime = timeout_block_height

    sighash = _sighash_taproot_script_path(
        tx,
        input_index=0,
        script_pubkeys=[script.Script(data=p2tr_spk)],
        values=[vout_amount],
        script_hex=refund_script_hex,
        leaf_version=leaf_version,
    )
    sig = privkey.schnorr_sign(sighash)._sig

    tx.vin[0].witness = Witness(
        items=[sig, bytes.fromhex(refund_script_hex), refund_cb]
    )
    return bytes.hex(tx.serialize())
