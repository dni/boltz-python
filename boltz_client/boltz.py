"""boltz_client main module - Boltz v2 API with taproot swaps"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from math import ceil, floor
from typing import Optional

import httpx

from .helpers import req_wrap
from .onchain import (
    create_claim_tx,
    create_key_pair,
    create_preimage,
    create_refund_tx,
    validate_address,
)


class SwapDirection(str, Enum):
    send = "send"
    receive = "receive"


class BoltzLimitException(Exception):
    pass


class BoltzApiException(Exception):
    pass


class BoltzAddressValidationException(Exception):
    pass


class BoltzNotFoundException(Exception):
    pass


class BoltzPairException(Exception):
    pass


class BoltzSwapStatusException(Exception):
    def __init__(self, message: str, status: str):
        self.message = message
        self.status = status


class BoltzSwapTransactionException(Exception):
    def __init__(self, message: str):
        self.message = message


@dataclass
class SwapTreeLeaf:
    output: str  # hex-encoded tapscript
    version: int  # leaf version (0xC0 = 192)


@dataclass
class SwapTree:
    claimLeaf: SwapTreeLeaf
    refundLeaf: SwapTreeLeaf

    @classmethod
    def from_dict(cls, d: dict) -> "SwapTree":
        return cls(
            claimLeaf=SwapTreeLeaf(**d["claimLeaf"]),
            refundLeaf=SwapTreeLeaf(**d["refundLeaf"]),
        )


@dataclass
class BoltzSwapStatusResponse:
    status: str
    failureReason: Optional[str] = None
    zeroConfRejected: Optional[bool] = None
    transaction: Optional[dict] = None


@dataclass
class BoltzSwapResponse:
    """Response from creating a submarine swap (on-chain → Lightning)."""

    id: str
    bip21: str
    address: str
    swapTree: SwapTree
    claimPublicKey: str  # Boltz's public key (used for MuSig2 internal key)
    acceptZeroConf: bool
    expectedAmount: int
    timeoutBlockHeight: int
    blindingKey: Optional[str] = None
    referralId: Optional[str] = None


@dataclass
class BoltzReverseSwapResponse:
    """Response from creating a reverse swap (Lightning → on-chain)."""

    id: str
    invoice: str
    swapTree: SwapTree
    refundPublicKey: str  # Boltz's public key (used for MuSig2 internal key)
    lockupAddress: str
    timeoutBlockHeight: int
    onchainAmount: int
    blindingKey: Optional[str] = None
    referralId: Optional[str] = None


@dataclass
class BoltzConfig:
    network: str = "main"
    network_liquid: str = "liquidv1"
    pairs: list = field(default_factory=lambda: ["BTC/BTC", "L-BTC/BTC"])
    api_url: str = "https://api.boltz.exchange"
    referral_id: str = "dni"


class BoltzClient:
    def __init__(self, config: BoltzConfig, pair: str = "BTC/BTC"):
        self._cfg = config
        if pair not in self._cfg.pairs:
            raise BoltzPairException(
                f"invalid pair {pair}, possible pairs: {', '.join(self._cfg.pairs)}"
            )
        self.pair = pair

        if self.pair == "L-BTC/BTC":
            self.network = self._cfg.network_liquid
            self._from_asset = "L-BTC"
        else:
            self.network = self._cfg.network
            self._from_asset = "BTC"

        self._sub_pair_data: Optional[dict] = None
        self._rev_pair_data: Optional[dict] = None

    def _base(self) -> str:
        return f"{self._cfg.api_url}/v2"

    def request(self, funcname: str, *args, **kwargs) -> dict:
        try:
            return req_wrap(funcname, *args, **kwargs)
        except httpx.RequestError as exc:
            msg = f"unreachable: {exc.request.url!r}."
            raise BoltzApiException(f"boltz api connection error: {msg}") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise BoltzNotFoundException(
                    exc.response.json().get("error", "not found")
                ) from exc
            msg = f"{exc.response.status_code} while requesting {exc.request.url!r}. message: {exc.response.json().get('error', '')}"
            raise BoltzApiException(f"boltz api status error: {msg}") from exc

    def _headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def _load_pair_data(self) -> None:
        if self._sub_pair_data is None:
            pairs = self.request("get", f"{self._base()}/swap/submarine", headers=self._headers())
            self._sub_pair_data = pairs.get(self._from_asset, {}).get("BTC", {})
        if self._rev_pair_data is None:
            pairs = self.request("get", f"{self._base()}/swap/reverse", headers=self._headers())
            self._rev_pair_data = pairs.get("BTC", {}).get(self._from_asset, {})

    @property
    def _sub_fees(self) -> dict:
        self._load_pair_data()
        return self._sub_pair_data.get("fees", {})  # type: ignore[union-attr]

    @property
    def _rev_fees(self) -> dict:
        self._load_pair_data()
        return self._rev_pair_data.get("fees", {})  # type: ignore[union-attr]

    @property
    def limits(self) -> dict:
        self._load_pair_data()
        return self._sub_pair_data.get("limits", {})  # type: ignore[union-attr]

    def add_reverse_swap_fees(self, amount: int) -> int:
        miner = self._rev_fees.get("minerFees", {})
        fee = miner.get("claim", 0) + miner.get("lockup", 0)
        percent = self._rev_fees.get("percentage", 0)
        return ceil((amount + fee) / (1 - (percent / 100)))

    def substract_swap_fees(self, amount: int) -> int:
        fee = self._sub_fees.get("minerFees", 0)
        percent = self._sub_fees.get("percentage", 0)
        return floor((amount - fee) / (1 + (percent / 100)))

    def get_fee_estimation_claim(self) -> int:
        return self._rev_fees.get("minerFees", {}).get("claim", 0)

    def get_fee_estimation_refund(self) -> int:
        return self._sub_fees.get("minerFees", 0)

    def check_limits(self, amount: int) -> None:
        lim = self.limits
        valid = lim.get("minimal", 0) <= amount <= lim.get("maximal", float("inf"))
        if not valid:
            raise BoltzLimitException(
                f"Boltz - swap not in boltz limits, amount: {amount}, "
                f"min: {lim.get('minimal')}, max: {lim.get('maximal')}"
            )

    def swap_status(self, boltz_id: str) -> BoltzSwapStatusResponse:
        data = self.request("get", f"{self._base()}/swap/{boltz_id}", headers=self._headers())
        status = BoltzSwapStatusResponse(
            status=data["status"],
            failureReason=data.get("failureReason"),
            zeroConfRejected=data.get("zeroConfRejected"),
            transaction=data.get("transaction"),
        )
        if status.failureReason:
            raise BoltzSwapStatusException(status.failureReason, status.status)
        return status

    def send_onchain_tx(self, rawtx: str) -> str:
        data = self.request(
            "post",
            f"{self._base()}/chain/{self._from_asset}/transaction",
            headers=self._headers(),
            json={"hex": rawtx},
        )
        return data["id"]

    def validate_address(self, address: str) -> str:
        try:
            return validate_address(address, self.network, self.pair)
        except ValueError as exc:
            raise BoltzAddressValidationException(exc) from exc

    async def wait_for_tx_on_status(self, boltz_id: str, zeroconf: bool = True) -> str:
        """Poll swap status until a lockup transaction appears (for reverse swap claim)."""
        while True:
            try:
                status = self.swap_status(boltz_id)
                assert status.transaction
                tx_hex = status.transaction.get("hex")
                assert tx_hex
                if not zeroconf:
                    assert status.status == "transaction.confirmed"
                return tx_hex
            except (BoltzApiException, BoltzSwapStatusException, AssertionError):
                await asyncio.sleep(3)

    async def wait_for_tx(self, boltz_id: str) -> str:
        """Poll for submarine swap lockup transaction (for refund after failed submarine swap)."""
        while True:
            try:
                data = self.request(
                    "get",
                    f"{self._base()}/swap/submarine/{boltz_id}/transaction",
                    headers=self._headers(),
                )
                tx_hex = data.get("hex")
                assert tx_hex
                return tx_hex
            except (BoltzApiException, BoltzNotFoundException, AssertionError):
                await asyncio.sleep(3)

    async def claim_reverse_swap(
        self,
        boltz_id: str,
        lockup_address: str,
        receive_address: str,
        privkey_wif: str,
        preimage_hex: str,
        swap_tree: SwapTree,
        boltz_pubkey: str,
        zeroconf: bool = True,
        blinding_key: Optional[str] = None,
    ) -> str:
        """Claim a reverse swap output (Lightning → on-chain) via taproot script path."""
        self.validate_address(receive_address)
        self.validate_address(lockup_address)
        lockup_rawtx = await self.wait_for_tx_on_status(boltz_id, zeroconf)

        transaction = create_claim_tx(
            lockup_address=lockup_address,
            lockup_rawtx=lockup_rawtx,
            receive_address=receive_address,
            privkey_wif=privkey_wif,
            preimage_hex=preimage_hex,
            claim_script_hex=swap_tree.claimLeaf.output,
            refund_script_hex=swap_tree.refundLeaf.output,
            boltz_pubkey=boltz_pubkey,
            fees=self.get_fee_estimation_claim(),
            pair=self.pair,
            leaf_version=swap_tree.claimLeaf.version,
            blinding_key=blinding_key,
        )
        return self.send_onchain_tx(transaction)

    async def refund_swap(
        self,
        boltz_id: str,
        privkey_wif: str,
        lockup_address: str,
        receive_address: str,
        swap_tree: SwapTree,
        boltz_pubkey: str,
        timeout_block_height: int,
        blinding_key: Optional[str] = None,
    ) -> str:
        """Refund a failed submarine swap via taproot script path."""
        self.validate_address(receive_address)
        self.validate_address(lockup_address)
        lockup_rawtx = await self.wait_for_tx(boltz_id)

        transaction = create_refund_tx(
            lockup_address=lockup_address,
            lockup_rawtx=lockup_rawtx,
            privkey_wif=privkey_wif,
            receive_address=receive_address,
            claim_script_hex=swap_tree.claimLeaf.output,
            refund_script_hex=swap_tree.refundLeaf.output,
            boltz_pubkey=boltz_pubkey,
            timeout_block_height=timeout_block_height,
            fees=self.get_fee_estimation_refund(),
            pair=self.pair,
            leaf_version=swap_tree.refundLeaf.version,
            blinding_key=blinding_key,
        )
        return self.send_onchain_tx(transaction)

    def create_swap(self, payment_request: str) -> tuple[str, BoltzSwapResponse]:
        """Create a submarine swap (on-chain → Lightning). Returns (privkey_wif, response)."""
        refund_privkey_wif, refund_pubkey_hex = create_key_pair(self.network, self.pair)
        data = self.request(
            "post",
            f"{self._base()}/swap/submarine",
            headers=self._headers(),
            json={
                "from": self._from_asset,
                "to": "BTC",
                "invoice": payment_request,
                "refundPublicKey": refund_pubkey_hex,
                "referralId": self._cfg.referral_id,
            },
        )
        swap = BoltzSwapResponse(
            id=data["id"],
            bip21=data.get("bip21", ""),
            address=data["address"],
            swapTree=SwapTree.from_dict(data["swapTree"]),
            claimPublicKey=data["claimPublicKey"],
            acceptZeroConf=data.get("acceptZeroConf", False),
            expectedAmount=data["expectedAmount"],
            timeoutBlockHeight=data["timeoutBlockHeight"],
            blindingKey=data.get("blindingKey"),
        )
        return refund_privkey_wif, swap

    def create_reverse_swap(self, amount: int = 0) -> tuple[str, str, BoltzReverseSwapResponse]:
        """Create a reverse swap (Lightning → on-chain). Returns (privkey_wif, preimage_hex, response)."""
        self.check_limits(amount)
        claim_privkey_wif, claim_pubkey_hex = create_key_pair(self.network, self.pair)
        preimage_hex, preimage_hash = create_preimage()
        data = self.request(
            "post",
            f"{self._base()}/swap/reverse",
            headers=self._headers(),
            json={
                "from": "BTC",
                "to": self._from_asset,
                "invoiceAmount": amount,
                "preimageHash": preimage_hash,
                "claimPublicKey": claim_pubkey_hex,
                "referralId": self._cfg.referral_id,
            },
        )
        swap = BoltzReverseSwapResponse(
            id=data["id"],
            invoice=data["invoice"],
            swapTree=SwapTree.from_dict(data["swapTree"]),
            refundPublicKey=data["refundPublicKey"],
            lockupAddress=data["lockupAddress"],
            timeoutBlockHeight=data["timeoutBlockHeight"],
            onchainAmount=data["onchainAmount"],
            blindingKey=data.get("blindingKey"),
        )
        return claim_privkey_wif, preimage_hex, swap
