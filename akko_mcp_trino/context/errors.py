"""What a context provider can raise.

The text of these errors stays in the server: an agent is told the status
(``akko_mcp_trino.context.status.status_of``), never the message.
"""


class ProviderError(Exception):
    """Any failure of a context provider."""


class ProviderUnavailable(ProviderError):
    """The catalogue could not answer in time, or at all."""


class ProviderRefused(ProviderError):
    """The catalogue refused the server's credentials or the request."""


class ProviderAnswerTooLarge(ProviderUnavailable):
    """The catalogue's answer to this call was larger than the provider accepts.

    The call is ``unavailable``, as for an outage, but the catalogue is not down: no retry
    pause opens, and the next call asks it again.
    """
