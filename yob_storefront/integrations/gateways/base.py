# Copyright (c) 2026, YOB and Shayona
"""The YOB gateway-driver boundary.

YOB owns the commercial payment lifecycle; a gateway driver owns only the
conversation with one external provider. The split, stated once:

    YOB            Cart, immutable Payment Request, Payment Method eligibility,
                   Cart -> Sales Order commitment, Pay Later, /payment/:token,
                   the durable commit before any provider call, settlement and
                   idempotency, and every public API contract.

    Frappe         Payment Gateway records, gateway Settings DocTypes,
    Payments       credentials, and any provider capability its controller
                   already satisfies.

    YOBGateway     the thin adapter between the two, per provider.

Deliberately NOT Razorpay-shaped. ``order_id`` is a Razorpay concept; Stripe
has no orders and PayPal has neither. So a driver returns a ``ProviderIntent``
carrying an opaque ``client_payload`` that only the matching browser SDK
understands, plus a provider-neutral ``provider_reference``. YOB core never
reads inside ``client_payload``; the public API adapts it per provider so
existing SPA contracts stay byte-identical.

An ``obligation`` is ALWAYS the already-committed pair:

    Payment Request (immutable) + Sales Order (committed)

A driver must never treat a Cart as payment truth. By the time a driver runs,
the Cart is Ordered and irrelevant -- the amount owed is on the Payment Request
and nowhere else.

Frappe Payments' hosted checkout is intentionally NOT used: ``get_payment_url``,
``create_request``, ``authorize_payment`` and the ``*_checkout`` pages all drive
a server-rendered redirect flow, and YOB has its own Angular SPA. Payments
remains the configuration and credential foundation; the browser experience is
YOB's.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

# --- capabilities ---------------------------------------------------------
#
# Advertised per driver so orchestration can assert what it depends on instead
# of assuming every provider behaves like Razorpay.

#: The driver can recover a previously created provider payment instead of
#: creating a duplicate (Razorpay: deterministic receipt lookup).
CAP_RECOVER = "recover"

#: The driver verifies the provider's callback server-side (e.g. HMAC).
CAP_SERVER_VERIFY = "server_verify"


class ProviderPreflightFailed(Exception):
    """A static prerequisite for using this gateway is not satisfied.

    Raised BEFORE any commercial commitment and before any network call, so
    nothing has happened yet and nothing needs undoing. Distinct from a provider
    failure, which happens after a real obligation exists and IS retryable.
    """

    def __init__(self, reason: str, code: str | None = None):
        super().__init__(reason)
        self.reason = reason
        #: Optional published error code the API layer should prefer.
        self.code = code


@dataclass(frozen=True)
class Obligation:
    """What is owed, and (once committed) the order it is owed against.

    ``sales_order`` is None during preflight, because preflight deliberately
    runs BEFORE the Cart is committed -- there is no order yet, and that is the
    whole point. Every other operation receives both documents, already durable:
    the caller committed the transaction before any provider call, so a provider
    failure can never roll back the Sales Order.

    A driver must never read ``sales_order`` during preflight.
    """

    payment_request: object
    sales_order: object = None

    @classmethod
    def pending(cls, payment_request) -> "Obligation":
        """Pre-commitment view: the immutable obligation, no order yet."""

        return cls(payment_request=payment_request, sales_order=None)

    @property
    def amount_minor(self) -> int:
        """Amount in the provider's smallest currency unit (paise, cents).

        Derived from the IMMUTABLE Payment Request, never from a Cart and never
        from the Sales Order -- the Sales Order is cross-checked against it by
        the caller, but the obligation is what the buyer agreed to pay.

        Decimal via str(), not float arithmetic: ``135.7 * 100`` is
        13569.999999999998 in binary floating point, and this value is money
        sent to a payment provider.

        NOTE for driver authors: this is the value to VERIFY a provider response
        against. It is NOT the value to send to the Frappe Payments controller,
        which takes major units and multiplies by 100 itself.
        """

        amount = Decimal(str(self.payment_request.grand_total or 0))

        return int(
            (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )

    @property
    def currency(self) -> str:
        return self.payment_request.currency

    @property
    def reference(self) -> str:
        """Stable identity of the obligation, used for provider idempotency."""

        return self.payment_request.name


@dataclass(frozen=True)
class ProviderIntent:
    """Everything the browser needs to start ONE payment attempt.

    ``client_payload`` is provider-specific and opaque to YOB core:

        Razorpay -> {"key": ..., "order_id": ...}
        Stripe   -> {"publishable_key": ..., "client_secret": ...}   (not built)

    ``provider_reference`` is the provider's own handle for the attempt
    (Razorpay order id, Stripe PaymentIntent id). YOB stores it for recovery and
    reconciliation but attaches no meaning to its format.
    """

    provider: str
    client_sdk: str
    provider_reference: str
    client_payload: dict
    amount_minor: int
    currency: str
    #: True when an existing provider payment was reused/recovered rather than
    #: newly created. Diagnostic only; the public API shape does not change.
    reused: bool = False


@dataclass(frozen=True)
class ProviderResult:
    """Outcome of verifying a provider callback."""

    provider: str
    settled: bool
    provider_reference: str
    provider_payment_reference: str
    amount_minor: int
    currency: str
    raw: dict = field(default_factory=dict)


class ProviderNotConfigured(Exception):
    """The gateway has no usable credentials/configuration on this site."""


class ProviderAlreadyPaid(Exception):
    """The provider already holds a captured/authorised payment."""


class ProviderError(Exception):
    """The provider could not be reached, or answered unusably.

    Raised only AFTER the obligation is durable, so it always means "retryable,
    and the Sales Order stands".
    """


class ProviderIntegrityError(Exception):
    """Provider state is ambiguous in a way only a human should resolve.

    Distinct from ProviderError: this is not retryable and not a transient
    fault. It means the provider holds records that YOB cannot safely choose
    between -- e.g. two attempted orders sharing one receipt -- and settling the
    wrong one would move real money against the wrong obligation.
    """


class UnsupportedProvider(Exception):
    """No driver is registered for the requested Payment Gateway."""


class YOBGateway:
    """One external payment provider, adapted to YOB's lifecycle.

    Subclasses implement the four operations below. They may rely on Frappe
    Payments for configuration and credentials, and may add provider-specific
    behaviour that Payments does not offer -- that is the explicit purpose of
    this layer, not a workaround.
    """

    #: Must equal the `gateway` value of the Frappe `Payment Gateway` record.
    provider: str = ""

    #: Which browser SDK the SPA should load for `client_payload`.
    client_sdk: str = ""

    def capabilities(self) -> frozenset:
        return frozenset()

    def assert_configured(self) -> None:
        """Raise ProviderNotConfigured when credentials are absent."""

        raise NotImplementedError

    def validate_currency(self, currency: str) -> None:
        """Raise ProviderPreflightFailed when the gateway cannot take this currency."""

        raise NotImplementedError

    def preflight(self, obligation: Obligation) -> None:
        """Every STATIC prerequisite for starting a payment on this gateway.

        Runs before the Cart is committed to a Sales Order, so a gateway that
        could never have taken this payment does not leave a real order behind.
        The distinction being drawn:

            preflight failure    nothing was ever possible -> no order, not
                                 retryable by repeating the same request
            provider failure     a real obligation exists and the network or
                                 the provider failed -> order stands, retryable

        Strictly non-network and side-effect free. It must not create a provider
        order or payment intent, open a checkout, or do anything irreversible.
        Reading local configuration -- including Frappe Payments controller
        settings -- is expected.

        Raises ProviderNotConfigured or ProviderPreflightFailed; returns None on
        success. ``obligation.sales_order`` is None here and must not be read.
        """

        self.assert_configured()
        self.validate_currency(obligation.currency)

    def prepare_payment(self, obligation: Obligation) -> ProviderIntent:
        """Create or RECOVER the one provider payment for this obligation.

        Must be idempotent per obligation: repeated calls converge on a single
        effective provider payment rather than creating another.
        """

        raise NotImplementedError

    def recover_payment(self, obligation: Obligation) -> ProviderIntent | None:
        """Find an existing provider payment for this obligation, or None.

        Separate from ``prepare_payment`` so recovery can be exercised and
        reasoned about on its own; ``prepare_payment`` is expected to use it.
        """

        raise NotImplementedError

    def verify_payment(self, obligation: Obligation,
                       provider_payload: dict) -> ProviderResult:
        """Verify a provider callback server-side. Never trust the browser."""

        raise NotImplementedError
