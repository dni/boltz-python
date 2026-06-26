"""boltz_client onchain module - Boltz v2 taproot"""

import hashlib
import os
import warnings
from hashlib import sha256

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="builtin type .* has no __module__ attribute",
        category=DeprecationWarning,
    )
    import wallycore as wally

from .onchain_wally import NETWORKS as WALLY_NETWORKS
from .onchain_wally import decode_address

_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_BECH32_ALPHABET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _tagged_hash(tag: str, data: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + data).digest()


def _compact_size(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + n.to_bytes(2, "little")
    if n <= 0xFFFFFFFF:
        return b"\xfe" + n.to_bytes(4, "little")
    return b"\xff" + n.to_bytes(8, "little")


def _btc_network(network: str) -> tuple[str, int]:
    networks = {
        "main": ("bc", wally.WALLY_NETWORK_BITCOIN_MAINNET),
        "test": ("tb", wally.WALLY_NETWORK_BITCOIN_TESTNET),
        "regtest": ("bcrt", wally.WALLY_NETWORK_BITCOIN_REGTEST),
    }
    return networks[network]


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]


def _bech32_polymod(values: list[int]) -> int:
    generators = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for idx, generator in enumerate(generators):
            if (top >> idx) & 1:
                checksum ^= generator
    return checksum


def _convert_bits(data: list[int], from_bits: int, to_bits: int) -> bytes:
    acc = 0
    bits = 0
    result = []
    max_value = (1 << to_bits) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            raise ValueError("Invalid bech32 data")
        acc = (acc << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((acc >> bits) & max_value)
    if bits >= from_bits or ((acc << (to_bits - bits)) & max_value):
        raise ValueError("Invalid bech32 padding")
    return bytes(result)


def _segwit_scriptpubkey(address: str, expected_hrp: str) -> bytes:
    address = address.lower()
    if not address.startswith(f"{expected_hrp}1"):
        raise ValueError("Invalid bech32 prefix")
    separator = address.rfind("1")
    hrp = address[:separator]
    data = [_BECH32_ALPHABET.find(char) for char in address[separator + 1 :]]
    if len(data) < 7 or any(value == -1 for value in data):
        raise ValueError("Invalid bech32 data")
    polymod = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    version = data[0]
    program = _convert_bits(data[1:-6], 5, 8)
    if version == 0:
        if polymod != 1 or len(program) not in [20, 32]:
            raise ValueError("Invalid bech32 witness program")
        return b"\x00" + bytes([len(program)]) + program
    if version == 1:
        if polymod != 0x2BC830A3 or len(program) != 32:
            raise ValueError("Invalid bech32m witness program")
        return b"\x51\x20" + program
    raise ValueError("Unsupported witness version")


def _liquid_network(network: str):
    liquid_networks = {
        "liquidv1": "mainnet",
        "liquidtestnet": "testnet",
        "elementsregtest": "regtest",
    }
    wally_network_name = liquid_networks[network]
    return next(
        liquid_network
        for liquid_network in WALLY_NETWORKS
        if liquid_network.name == wally_network_name
    )


def _address_to_scriptpubkey(address: str, network: str) -> bytes:
    bech32_prefix, wally_network = _btc_network(network)
    try:
        return _segwit_scriptpubkey(address, bech32_prefix)
    except ValueError:
        return wally.address_to_scriptpubkey(address, wally_network)


def validate_address(address: str, network: str, pair: str) -> str:
    try:
        if pair == "L-BTC/BTC":
            decode_address(wally, _liquid_network(network), address)
        else:
            _address_to_scriptpubkey(address, network)
    except Exception as exc:
        raise ValueError(f"Invalid address: {exc}") from exc
    return address


def create_preimage() -> tuple[str, str]:
    preimage = os.urandom(32)
    preimage_hash = sha256(preimage).hexdigest()
    return preimage.hex(), preimage_hash


def create_key_pair(network: str, pair: str) -> tuple[str, str]:
    while True:
        privkey = os.urandom(32)
        try:
            wally.ec_private_key_verify(privkey)
            break
        except ValueError:
            continue

    if pair == "BTC/BTC" and network == "main":
        wif_prefix = wally.WALLY_ADDRESS_VERSION_WIF_MAINNET
    else:
        wif_prefix = wally.WALLY_ADDRESS_VERSION_WIF_TESTNET

    pubkey_hex = wally.ec_public_key_from_private_key(privkey).hex()
    privkey_wif = wally.wif_from_bytes(
        privkey,
        wif_prefix,
        wally.WALLY_WIF_FLAG_COMPRESSED,
    )
    return privkey_wif, pubkey_hex


def _tap_leaf_hash(script_hex: str, version: int = 0xC0) -> bytes:
    script_bytes = bytes.fromhex(script_hex)
    return _tagged_hash("TapLeaf", bytes([version]) + _compact_size(len(script_bytes)) + script_bytes)


def _tap_branch_hash(left: bytes, right: bytes) -> bytes:
    if right < left:
        left, right = right, left
    return _tagged_hash("TapBranch", left + right)


def _parse_pubkey(pubkey: bytes) -> tuple[int, int]:
    if len(pubkey) != 33 or pubkey[0] not in [2, 3]:
        raise ValueError("Expected compressed secp256k1 public key")
    x = int.from_bytes(pubkey[1:], "big")
    y_sq = (pow(x, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if y % 2 != pubkey[0] % 2:
        y = _P - y
    return x, y


def _point_add(
    p1: tuple[int, int] | None,
    p2: tuple[int, int] | None,
) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1

    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p1 == p2:
        slope = (3 * x1 * x1) * pow(2 * y1, -1, _P)
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, _P)
    slope %= _P
    x3 = (slope * slope - x1 - x2) % _P
    y3 = (slope * (x1 - x3) - y1) % _P
    return x3, y3


def _point_mul(scalar: int, point: tuple[int, int]) -> tuple[int, int] | None:
    result = None
    addend: tuple[int, int] | None = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _serialize_pubkey(point: tuple[int, int]) -> bytes:
    x, y = point
    return bytes([2 + (y % 2)]) + x.to_bytes(32, "big")


def _musig2_key_agg(pk1_hex: str, pk2_hex: str) -> bytes:
    """BIP327 key aggregation for 2 pubkeys (33-byte compressed hex)."""
    pk1 = bytes.fromhex(pk1_hex)
    pk2 = bytes.fromhex(pk2_hex)
    key_agg_hash = _tagged_hash("KeyAgg list", pk1 + pk2)
    a1 = int.from_bytes(_tagged_hash("KeyAgg coefficient", key_agg_hash + pk1), "big") % _N

    point1 = _point_mul(a1, _parse_pubkey(pk1))
    point2 = _parse_pubkey(pk2)
    aggregate = _point_add(point1, point2)
    if aggregate is None:
        raise ValueError("Invalid aggregate public key")
    return _serialize_pubkey(aggregate)[1:]


def _build_taproot(
    internal_xonly: bytes,
    claim_script_hex: str,
    refund_script_hex: str,
    leaf_version: int = 0xC0,
) -> tuple[bytes, bytes, bytes, bytes]:
    claim_h = _tap_leaf_hash(claim_script_hex, leaf_version)
    refund_h = _tap_leaf_hash(refund_script_hex, leaf_version)
    tree_hash = _tap_branch_hash(claim_h, refund_h)

    tweak = _tagged_hash("TapTweak", internal_xonly + tree_hash)
    out_compressed = wally.ec_public_key_bip341_tweak(
        b"\x02" + internal_xonly,
        tweak,
        0,
    )
    out_xonly = out_compressed[1:]
    parity = 0x01 if out_compressed[0] == 0x03 else 0x00

    p2tr_spk = b"\x51\x20" + out_xonly
    cb_prefix = bytes([leaf_version | parity]) + internal_xonly
    claim_cb = cb_prefix + refund_h
    refund_cb = cb_prefix + claim_h

    return out_xonly, p2tr_spk, claim_cb, refund_cb


def _find_utxo(lockup_rawtx: str, lockup_address: str) -> tuple[bytes, int, int]:
    try:
        lockup_tx = wally.tx_from_hex(lockup_rawtx, 0)
    except ValueError as exc:
        raise ValueError("Invalid lockup transaction hex") from exc

    lockup_spk = _address_to_scriptpubkey(lockup_address, "regtest")
    for vout in range(wally.tx_get_num_outputs(lockup_tx)):
        if wally.tx_get_output_script(lockup_tx, vout) == lockup_spk:
            return (
                wally.tx_get_txid(lockup_tx),
                vout,
                wally.tx_get_output_satoshi(lockup_tx, vout),
            )

    raise ValueError("No matching vout found in lockup transaction for lockup_address")


def _hash_prevouts(tx) -> bytes:
    data = b""
    for idx in range(wally.tx_get_num_inputs(tx)):
        data += wally.tx_get_input_txhash(tx, idx)
        data += wally.tx_get_input_index(tx, idx).to_bytes(4, "little")
    return hashlib.sha256(data).digest()


def _hash_amounts(values: list[int]) -> bytes:
    return hashlib.sha256(b"".join(value.to_bytes(8, "little") for value in values)).digest()


def _hash_scriptpubkeys(scriptpubkeys: list[bytes]) -> bytes:
    return hashlib.sha256(
        b"".join(_compact_size(len(scriptpubkey)) + scriptpubkey for scriptpubkey in scriptpubkeys)
    ).digest()


def _hash_sequence(tx) -> bytes:
    data = b""
    for idx in range(wally.tx_get_num_inputs(tx)):
        data += wally.tx_get_input_sequence(tx, idx).to_bytes(4, "little")
    return hashlib.sha256(data).digest()


def _hash_outputs(tx) -> bytes:
    data = b""
    for idx in range(wally.tx_get_num_outputs(tx)):
        scriptpubkey = wally.tx_get_output_script(tx, idx)
        data += wally.tx_get_output_satoshi(tx, idx).to_bytes(8, "little")
        data += _compact_size(len(scriptpubkey)) + scriptpubkey
    return hashlib.sha256(data).digest()


def _sighash_taproot_script_path(
    tx,
    input_index: int,
    script_pubkeys: list[bytes],
    values: list[int],
    script_hex: str,
    leaf_version: int = 0xC0,
    sighash: int = 0,
) -> bytes:
    if sighash != 0:
        raise ValueError("Unsupported taproot sighash type")

    h = hashlib.sha256()
    h.update(_tagged_hash("TapSighash", b""))
    h.update(_tagged_hash("TapSighash", b""))
    h.update(b"\x00")
    h.update(bytes([sighash]))
    h.update(wally.tx_get_version(tx).to_bytes(4, "little"))
    h.update(wally.tx_get_locktime(tx).to_bytes(4, "little"))
    h.update(_hash_prevouts(tx))
    h.update(_hash_amounts(values))
    h.update(_hash_scriptpubkeys(script_pubkeys))
    h.update(_hash_sequence(tx))
    h.update(_hash_outputs(tx))
    h.update(b"\x02")  # spend_type: ext_flag=1, annex_present=0
    h.update(input_index.to_bytes(4, "little"))
    h.update(_tap_leaf_hash(script_hex, leaf_version))
    h.update(b"\x00")  # key_version
    h.update((0xFFFFFFFF).to_bytes(4, "little"))  # code_separator_pos
    return h.digest()


def _sign_tx(
    tx,
    privkey: bytes,
    p2tr_spk: bytes,
    vout_amount: int,
    script_hex: str,
    leaf_version: int,
) -> bytes:
    sighash = _sighash_taproot_script_path(
        tx,
        input_index=0,
        script_pubkeys=[p2tr_spk],
        values=[vout_amount],
        script_hex=script_hex,
        leaf_version=leaf_version,
    )
    return wally.ec_sig_from_bytes(sighash, privkey, wally.EC_FLAG_SCHNORR)


def _create_btc_tx(
    txid: bytes,
    vout_idx: int,
    vout_amount: int,
    receive_address: str,
    fees: int,
    sequence: int,
    locktime: int = 0,
):
    tx = wally.tx_init(2, locktime, 1, 1)
    vin = wally.tx_input_init(txid, vout_idx, sequence, None, None)
    vout = wally.tx_output_init(
        vout_amount - fees,
        _address_to_scriptpubkey(receive_address, "regtest"),
    )
    wally.tx_add_input(tx, vin)
    wally.tx_add_output(tx, vout)
    return tx


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
    if pair == "L-BTC/BTC":
        raise NotImplementedError("Liquid taproot claim not yet implemented for v2")

    privkey = wally.wif_to_bytes(
        privkey_wif,
        wally.WALLY_ADDRESS_VERSION_WIF_TESTNET,
        wally.WALLY_WIF_FLAG_COMPRESSED,
    )
    our_pubkey = wally.ec_public_key_from_private_key(privkey).hex()

    internal_xonly = _musig2_key_agg(boltz_pubkey, our_pubkey)
    _out_xonly, p2tr_spk, claim_cb, _ = _build_taproot(
        internal_xonly, claim_script_hex, refund_script_hex, leaf_version
    )

    expected_spk = _address_to_scriptpubkey(lockup_address, "regtest")
    if p2tr_spk != expected_spk:
        raise ValueError(
            "Computed taproot address does not match lockup_address; "
            "check key ordering or leaf scripts"
        )

    txid, vout_idx, vout_amount = _find_utxo(lockup_rawtx, lockup_address)
    tx = _create_btc_tx(
        txid,
        vout_idx,
        vout_amount,
        receive_address,
        fees,
        sequence=0xFFFFFFFF,
    )
    sig = _sign_tx(tx, privkey, p2tr_spk, vout_amount, claim_script_hex, leaf_version)

    witness = wally.tx_witness_stack_init(4)
    for item in [
        sig,
        bytes.fromhex(preimage_hex),
        bytes.fromhex(claim_script_hex),
        claim_cb,
    ]:
        wally.tx_witness_stack_add(witness, item)
    wally.tx_set_input_witness(tx, 0, witness)
    return wally.tx_to_hex(tx, wally.WALLY_TX_FLAG_USE_WITNESS)


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
    if pair == "L-BTC/BTC":
        raise NotImplementedError("Liquid taproot refund not yet implemented for v2")

    privkey = wally.wif_to_bytes(
        privkey_wif,
        wally.WALLY_ADDRESS_VERSION_WIF_TESTNET,
        wally.WALLY_WIF_FLAG_COMPRESSED,
    )
    our_pubkey = wally.ec_public_key_from_private_key(privkey).hex()

    internal_xonly = _musig2_key_agg(boltz_pubkey, our_pubkey)
    _out_xonly, p2tr_spk, _, refund_cb = _build_taproot(
        internal_xonly, claim_script_hex, refund_script_hex, leaf_version
    )

    expected_spk = _address_to_scriptpubkey(lockup_address, "regtest")
    if p2tr_spk != expected_spk:
        raise ValueError(
            "Computed taproot address does not match lockup_address; "
            "check key ordering or leaf scripts"
        )

    txid, vout_idx, vout_amount = _find_utxo(lockup_rawtx, lockup_address)
    tx = _create_btc_tx(
        txid,
        vout_idx,
        vout_amount,
        receive_address,
        fees,
        sequence=0xFFFFFFFE,
        locktime=timeout_block_height,
    )
    sig = _sign_tx(tx, privkey, p2tr_spk, vout_amount, refund_script_hex, leaf_version)

    witness = wally.tx_witness_stack_init(3)
    for item in [sig, bytes.fromhex(refund_script_hex), refund_cb]:
        wally.tx_witness_stack_add(witness, item)
    wally.tx_set_input_witness(tx, 0, witness)
    return wally.tx_to_hex(tx, wally.WALLY_TX_FLAG_USE_WITNESS)
