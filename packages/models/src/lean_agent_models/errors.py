"""What can go wrong talking to a model backend, as a closed set.

Deliberately shaped like `ReplWorker`'s crash taxonomy (M1.8.2) rather than one flat exception,
and for the same reason: spec's "`infra_error` is a distinct, unbudgeted outcome from a proof
failure" applies one layer down here too. A model that timed out says nothing about whether the
obligation is provable; a model that answered with an unusable shape says nothing about it either.
Neither should be charged as a failed proof attempt, and the executor can only make that
distinction if the types make it first.
"""

from __future__ import annotations


class ModelBackendError(Exception):
    """Base for every condition that means this request produced no usable completion."""


class ModelTimeout(ModelBackendError):
    """No response within the request's wallclock budget."""


class ModelUnavailable(ModelBackendError):
    """The endpoint could not be reached, or answered with a server error.

    Distinct from `ModelTimeout` because the responses differ: a timeout may mean the request was
    too large for the budget, while unavailability is about the deployment and no smaller request
    would help.
    """


class ModelProtocolError(ModelBackendError):
    """The backend answered, and the answer cannot be used.

    The case this exists for is specific and was observed rather than imagined: **a server that
    accepts `logprobs` and silently returns none.** Ollama's OpenAI-compatible layer does exactly
    that -- its native API computes logprobs, its OpenAI shim drops the field without erroring.
    Spec §6.5 requires storing the sampled token's logprob, and §9 lists logprobs among the things
    that cannot be recomputed later, so a response missing them is not a degraded success. Silently
    accepting one would write `trajectory.logprobs_blob` as NULL and the loss would surface only
    when training needs behaviour-policy logprobs that no longer exist.

    Also covers a completion whose `logprobs` array is not parallel to its token ids, and a
    response with no choices at all.
    """
