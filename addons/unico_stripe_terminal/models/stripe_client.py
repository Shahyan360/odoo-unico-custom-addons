import logging

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

try:
    import stripe
except (ImportError, IOError) as ex:  # pragma: no cover
    stripe = None
    _logger.warning("The `stripe` python library is not installed: %s", ex)


# Stripe expects amounts in the smallest currency unit (e.g. cents). Currencies
# such as JPY are "zero-decimal" - they must be sent as whole units.
ZERO_DECIMAL_CURRENCIES = {
    "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG",
    "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF",
}


def terminal_user_error(env, error=None, *, code=None, details=None, stage="start"):
    """Build a ``UserError`` for a failed reader command.

    Known Terminal error codes get cashier-friendly guidance; anything else
    falls back to a generic message. The Stripe code, request ID and raw
    message are appended as technical details so support can trace the request.
    """
    _ = env._
    code = code or getattr(error, "code", None)
    if details is None:
        ref = " · ".join(filter(None, (code, getattr(error, "request_id", None))))
        raw = getattr(error, "user_message", None) or (str(error) if error else "")
        details = "\n".join(filter(None, (ref and f"Ref: {ref}", raw)))
    guidance = {
        "terminal_reader_timeout": _(
            "The payment terminal didn't respond in time.\n\n"
            "Check the terminal screen before trying again:\n"
            "• If it shows the payment screen, let the customer finish paying.\n"
            "• If it's back on its home screen, try again.\n\n"
            "If this keeps happening, check the terminal's Wi-Fi or network connection."
        ),
        "terminal_reader_busy": _(
            "The payment terminal is busy with another action.\n\n"
            "Wait for it to finish, or cancel it on the terminal screen, "
            "then try again."
        ),
        "terminal_reader_offline": _(
            "The payment terminal isn't connected.\n\n"
            "Make sure it's powered on and connected to Wi-Fi or the network. "
            "Once it shows its home screen, try again."
        ),
    }
    if code in guidance:
        message = guidance[code]
    elif stage == "confirm":
        message = _(
            "Something went wrong while completing the payment on the terminal. "
            "Check the terminal screen and the payment status before trying "
            "again, or contact support if it keeps happening."
        )
    else:
        message = _(
            "Something went wrong while starting the payment on the terminal. "
            "Please try again, or contact support if it keeps happening."
        )
    return UserError(f"{message}\n\n{details}" if details else message)


def get_stripe(provider):
    """Return the `stripe` module configured with ``provider``'s secret key.

    ``provider`` is a ``payment.provider`` record (the Odoo Stripe provider from
    ``payment_stripe``). Raises a user-facing error when the library is missing
    or the provider has no secret key.
    """
    # Translate via ``provider.env`` rather than the global ``_``: this runs from
    # a cron (no web request / UI language), where the global ``_`` cannot detect
    # a language by frame inspection and logs a "no translation language" warning.
    if stripe is None:
        raise UserError(provider.env._(
            "The Python library `stripe` is not installed. "
            "Install it (pip install stripe) and restart Odoo."
        ))
    provider = provider.sudo()
    secret_key = provider.stripe_secret_key
    if not secret_key:
        raise UserError(provider.env._(
            "The Stripe payment provider has no Secret Key. Open it under "
            "Sales > Configuration > Payment Providers and fill in your keys."
        ))
    stripe.api_key = secret_key
    return stripe


def get_stripe_for_company(env, company):
    """Resolve the active Stripe provider for ``company`` and return
    ``(stripe_module, provider)``."""
    provider = env["payment.provider"]._get_terminal_provider(company)
    return get_stripe(provider), provider


def sget(obj, key, default=None):
    """Safely read ``key`` from a Stripe response or a plain dict.

    stripe-python >= 8 returns ``StripeObject`` instances that do **not**
    implement ``.get()`` but do support subscription (``obj["id"]``), raising
    ``KeyError`` for missing keys - exactly like a dict. This helper works
    uniformly for both, returning ``default`` when the key is absent or None.
    """
    if obj is None:
        return default
    try:
        value = obj[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def to_stripe_amount(amount, currency):
    """Convert an Odoo monetary ``amount`` to Stripe's integer minor units."""
    code = (currency.name or "").upper()
    if code in ZERO_DECIMAL_CURRENCIES:
        return int(round(amount))
    return int(round(amount * 100.0))


def from_stripe_amount(amount, currency):
    """Convert a Stripe integer minor-unit ``amount`` back to a float."""
    code = (currency.name or "").upper()
    if code in ZERO_DECIMAL_CURRENCIES:
        return float(amount)
    return float(amount) / 100.0
