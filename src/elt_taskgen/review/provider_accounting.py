"""Provider rate cards, conservative reservations, and trajectory cost accounting."""

from __future__ import annotations

import math
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from elt_taskgen.review import provider_types as _review_provider_types


class BudgetExceededError(RuntimeError):
    """Signal a task or total budget breach before or after a live attempt.

    Reserve failures occur before transport; charge failures occur after the paid
    transcript is recorded. The exception carries only numeric budget metadata and
    scope.
    """

    def __init__(self, message: str = "", *, scope: str = "task"):
        super().__init__(message)
        self.scope = str(scope)


class RoleCapExceeded(BudgetExceededError):
    """Signal that a trajectory's own USD cap cannot absorb or has been crossed by a turn.

    `scope` is always `role`. The session maps this to a USD limit stop rather than
    infrastructure failure; reserve failures spend nothing, while charge failures follow
    a recorded paid attempt.
    """

    def __init__(self, message: str = "", *, scope: str = "role"):
        if str(scope) != "role":
            raise ValueError(
                f"RoleCapExceeded is the role-scope breach only, got scope {scope!r}"
            )
        super().__init__(message, scope="role")


# Cost metering + budgets

@dataclass(frozen=True)
class RateCard:
    """Store per-million-token rates for input, cache writes, cache reads, and output.

    Rates are converted through `Decimal` and must be finite and nonnegative.
    """

    input: float
    cache_write: float
    cache_read: float
    output: float

    def __post_init__(self) -> None:
        """Validate rates required for conservative prospective accounting.

        All rates must be finite and nonnegative, and cache rates may not exceed
        uncached input unless the configured accounting explicitly supports that
        ordering.
        """

        for name in ("input", "cache_write", "cache_read", "output"):
            raw = getattr(self, name)
            if isinstance(raw, bool):
                raise ValueError(
                    f"rate card {name} must be a finite, non-negative "
                    "USD-per-million-token amount"
                )
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"rate card {name} must be a finite, non-negative "
                    "USD-per-million-token amount"
                ) from exc
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(
                    f"rate card {name} must be a finite, non-negative "
                    "USD-per-million-token amount"
                )
            object.__setattr__(self, name, value)

    @classmethod
    def uncached(cls, usd_in: float, usd_out: float) -> "RateCard":
        return cls(
            input=float(usd_in),
            cache_write=float(usd_in),
            cache_read=float(usd_in),
            output=float(usd_out),
        )

    @classmethod
    def coerce(cls, rates: Any) -> "RateCard":
        """A RateCard from a RateCard, a legacy (input, output) pair or a
        four-tuple in card order."""
        if isinstance(rates, RateCard):
            return rates
        if isinstance(rates, (tuple, list)):
            if len(rates) == 2:
                return cls.uncached(float(rates[0]), float(rates[1]))
            if len(rates) == 4:
                return cls(*(float(r) for r in rates))
        raise TypeError(
            f"rates must be a RateCard, an (input, output) pair or a "
            f"four-rate tuple, got {rates!r}"
        )

    def as_pair(self) -> tuple[float, float]:
        """The legacy (input, output) view."""
        return (self.input, self.output)

    def usd_for(self, usage: _review_provider_types.Usage) -> float:
        """List-price USD of one usage at these four rates."""
        return (
            usage.input_tokens * self.input
            + usage.cache_creation_input_tokens * self.cache_write
            + usage.cache_read_input_tokens * self.cache_read
            + usage.output_tokens * self.output
        ) / 1_000_000.0


#: Anthropic USD per million tokens: input, cache write, cache read, output.
ANTHROPIC_PRICING_USD_PER_MTOK: dict[str, RateCard] = {
    "claude-opus-5": RateCard(5.00, 6.25, 0.50, 25.00),
    "claude-sonnet-5": RateCard(2.00, 2.50, 0.20, 10.00),
    "claude-haiku-4-5": RateCard(1.00, 1.25, 0.10, 5.00),
}

#: Default per-task circuit breaker; profiles may set a higher explicit limit.
DEFAULT_BUDGET_PER_TASK_USD = 5.00

#: Rough tokens-per-character used ONLY for the pre-flight reserve floor.
CHARS_PER_TOKEN_ESTIMATE = 4.0

#: Share of `max_tokens` a session turn is presumed to spend on output when
#: the previous turn's output was smaller (the state-machine reserve rule).
RESERVE_OUTPUT_FRACTION = 0.25


#: How to price a NEW openai_compat model. Keep this and the config comment
#: saying the same thing — it is the operator-facing contract.
OPENAI_COMPAT_PRICING_HOWTO = (
    "add it under providers.openai_compat.pricing in config/agents.yaml:\n"
    "  providers:\n"
    "    openai_compat:\n"
    "      pricing:\n"
    "        '<model-id>':\n"
    "          usd_per_mtok_input: <in>\n"
    "          usd_per_mtok_output: <out>\n"
    "          usd_per_mtok_cache_read: <optional; defaults to <in>>\n"
    "          usd_per_mtok_cache_write: <optional; defaults to <in>>\n"
    "Rates are USD per MILLION tokens. For an OpenRouter model, read them "
    "from https://openrouter.ai/api/v1/models — the `pricing.prompt` / "
    "`pricing.completion` fields are USD per SINGLE token, so multiply by "
    "1e6. A genuinely free endpoint (self-hosted vLLM) must still say so "
    "explicitly with 0.0/0.0: an omitted price is an oversight, an explicit "
    "zero is a decision."
)


def _explicit_rate(config: Mapping[str, Any], key: str) -> float | None:
    """A configured rate, or None when the key is absent/blank.

    `float(cfg.get(k) or 0.0)` cannot tell an explicit 0.0 from a missing key —
    "this endpoint is free" versus "nobody priced it".
    """
    if key not in config:
        return None
    value = config[key]
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(
            f"configured rate {key} must be a finite, non-negative "
            "USD-per-million-token amount"
        )
    return float(value)


def _openai_compat_rates(
    model: str, provider_config: Mapping[str, Any]
) -> RateCard | None:
    """Configured rates for one openai_compat model as a RateCard, else None.

    Per-model `pricing:` map first (a gateway serves many models at many
    prices), then endpoint-wide rates. Both halves (input, output) must be
    stated: half a price is not a price. The cache rates are optional and
    default to the input rate (uncached pricing never under-counts).
    """

    def card(config: Mapping[str, Any]) -> RateCard | None:
        rate_in = _explicit_rate(config, "usd_per_mtok_input")
        rate_out = _explicit_rate(config, "usd_per_mtok_output")
        if rate_in is None or rate_out is None:
            return None
        cache_read = _explicit_rate(config, "usd_per_mtok_cache_read")
        cache_write = _explicit_rate(config, "usd_per_mtok_cache_write")
        return RateCard(
            input=rate_in,
            cache_write=rate_in if cache_write is None else cache_write,
            cache_read=rate_in if cache_read is None else cache_read,
            output=rate_out,
        )

    pricing = provider_config.get("pricing")
    if isinstance(pricing, Mapping) and model:
        entry = pricing.get(model)
        if isinstance(entry, Mapping):
            found = card(entry)
            if found is not None:
                return found
    return card(provider_config)


def rate_card_for(
    provider: str, model: str, provider_config: Mapping[str, Any]
) -> RateCard:
    """The four-rate card for one route.

    FAIL CLOSED ON BOTH PROVIDERS: an unpriced model raises rather than metering
    $0, which is how a paid route gets reported as free and every budget claim
    for the run comes out wrong. A provider-reported cost may override these
    rates later but never excuse not having them.
    """
    if provider == "anthropic":
        rates = ANTHROPIC_PRICING_USD_PER_MTOK.get(model)
        if rates is None:
            raise RuntimeError(
                f"no pricing entry for anthropic model {model!r}; add it to "
                "ANTHROPIC_PRICING_USD_PER_MTOK (budgets must never "
                "silently undercount)"
            )
        return rates
    if provider == "openai_compat":
        rates = _openai_compat_rates(model, provider_config)
        if rates is None:
            raise RuntimeError(
                f"no pricing entry for openai_compat model {model!r} "
                f"(budgets must never silently undercount) — "
                f"{OPENAI_COMPAT_PRICING_HOWTO}"
            )
        return rates
    if provider == "claude_headless":
        # The harness serves Anthropic models: the Anthropic table, unless the
        # provider block declares its own rates for the model.
        rates = _openai_compat_rates(model, provider_config)
        if rates is None:
            rates = ANTHROPIC_PRICING_USD_PER_MTOK.get(model)
        if rates is None:
            raise RuntimeError(
                f"no pricing entry for claude_headless model {model!r}; add it to "
                "ANTHROPIC_PRICING_USD_PER_MTOK or declare `pricing:` under the "
                "claude_headless provider block (budgets must never silently undercount)"
            )
        return rates
    if provider == "codex_headless":
        rates = _openai_compat_rates(model, provider_config)
        if rates is None:
            raise RuntimeError(
                f"no pricing entry for codex_headless model {model!r} (budgets must "
                "never silently undercount) — declare `pricing:` under the "
                "codex_headless provider block, USD per million tokens"
            )
        return rates
    raise RuntimeError(  # unreachable: load_role_routing validates providers
        f"no pricing rule for provider {provider!r}"
    )


def rates_for(provider: str, model: str, provider_config: Mapping[str, Any]) -> tuple[float, float]:
    """(usd_per_mtok_input, usd_per_mtok_output) for one route — the legacy
    two-rate view of `rate_card_for`, same fail-closed rule."""
    return rate_card_for(provider, model, provider_config).as_pair()


def estimate_turn_usd(
    *,
    rates: Any,
    max_tokens: int,
    prompt: str = "",
    last_usage: _review_provider_types.Usage | None = None,
) -> float:
    """Estimate the amount reserved before a model call.

    The first turn uses the configured prompt floor. Later turns combine prior input
    cost with at least one quarter of the maximum output allowance, using uncached input
    rates for a conservative reservation.
    """
    card = RateCard.coerce(rates)
    if last_usage is not None:
        input_tokens = (
            last_usage.input_tokens
            + last_usage.cache_read_input_tokens
            + last_usage.cache_creation_input_tokens
        )
        output_tokens = max(
            last_usage.output_tokens,
            RESERVE_OUTPUT_FRACTION * max(0, int(max_tokens)),
        )
        return (input_tokens * card.input + output_tokens * card.output) / 1_000_000.0
    prompt_tokens = math.ceil(len(prompt) / CHARS_PER_TOKEN_ESTIMATE) if prompt else 0
    return prompt_tokens * card.input / 1_000_000.0


def _usable_report(reported_usd: float | None) -> float | None:
    """A provider-reported figure worth trusting, else None (a negative or
    non-finite report is ignored rather than trusted)."""
    if reported_usd is None:
        return None
    try:
        value = float(reported_usd)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


def _fresh_role_reading() -> dict[str, float]:
    return {
        "input_tokens": 0.0,
        "output_tokens": 0.0,
        "cache_read_input_tokens": 0.0,
        "cache_creation_input_tokens": 0.0,
        "usd": 0.0,
        "attempts": 0.0,
        "elapsed_ms": 0.0,
    }


class CostMeter:
    """Track tokens and USD under task, total, and role caps.

    `reserve` rejects estimated breaches before transport. `charge` records every API
    attempt after the exchange, including attempts in a backend failure, and may report
    a post-call breach.
    """

    def __init__(
        self,
        *,
        budget_per_task_usd: float = DEFAULT_BUDGET_PER_TASK_USD,
        budget_total_usd: float | None = None,
        budget_per_role_usd: Mapping[str, float] | None = None,
        durable_ledger: Any | None = None,
        durable_reservation_multiplier: float = 1.0,
    ):
        # One meter is intentionally shared by all RoutedProviders in a
        # metrology worker pool. Keep each read/modify/write and each
        # account-then-enforce decision atomic; an RLock lets
        # TrajectoryBudget hold the transaction while calling the meter's
        # internal helpers without introducing a second lock order.
        self._lock = threading.RLock()
        self.budget_per_task_usd = budget_per_task_usd
        self.budget_total_usd = budget_total_usd
        self.budget_per_role_usd: dict[str, float] = {
            str(role): float(cap)
            for role, cap in dict(budget_per_role_usd or {}).items()
            if cap is not None
        }
        self.total_usd = 0.0
        self.per_task_usd: dict[str, float] = {}
        #: Per-role readings: the four token counters, usd, attempts and the
        #: harness-measured transport wall.
        self.per_role: dict[str, dict[str, float]] = {}
        self.attempt_count = 0
        self.durable_ledger = durable_ledger
        multiplier = float(durable_reservation_multiplier)
        if not math.isfinite(multiplier) or multiplier < 1.0:
            raise ValueError("durable_reservation_multiplier must be finite and >= 1")
        self.durable_reservation_multiplier = multiplier

    # -- caps and headroom ----------------------------------------------------

    def role_cap_usd(self, role_name: str) -> float | None:
        """The declared per-trajectory cap for a role, None when uncapped."""
        return self.budget_per_role_usd.get(role_name)

    def headroom_usd(self, task_id: str) -> float | None:
        """USD still spendable on `task_id` under the task and total budgets
        (None when both are unlimited)."""
        with self._lock:
            limits: list[float] = []
            if self.budget_per_task_usd is not None:
                limits.append(
                    float(self.budget_per_task_usd) - self.per_task_usd.get(task_id, 0.0)
                )
            if self.budget_total_usd is not None:
                limits.append(float(self.budget_total_usd) - self.total_usd)
            return min(limits) if limits else None

    def trajectory(
        self,
        task_id: str,
        role_name: str,
        *,
        max_usd: float | None = None,
        enforce_role_cap: bool = True,
    ) -> "TrajectoryBudget":
        """The per-(task, role, trajectory) budget object; `max_usd` overrides
        the role's declared cap for this trajectory only. `enforce_role_cap=
        False` builds the trajectory of a ONE-SHOT exchange for a role whose
        `session:` block is declared but disabled: the meter's role cap is
        ignored for it (see TrajectoryBudget)."""
        return TrajectoryBudget(
            self,
            task_id=task_id,
            role_name=role_name,
            max_usd=max_usd,
            enforce_role_cap=enforce_role_cap,
        )

    def reserve(
        self,
        *,
        task_id: str,
        role_name: str,
        est_usd: float,
        trajectory_spent_usd: float = 0.0,
        max_usd: float | None = None,
    ) -> None:
        """Reject an estimated call that would exceed task, total, or role headroom.

        Run before transport and do not mutate charged totals. Durable-ledger
        reservation is coordinated atomically when configured.
        """
        with self._lock:
            est = max(0.0, float(est_usd))
            spent_task = self.per_task_usd.get(task_id, 0.0)
            if (
                self.budget_per_task_usd is not None
                and spent_task + est > float(self.budget_per_task_usd)
            ):
                raise BudgetExceededError(
                    f"per-task budget cannot absorb the next call for task "
                    f"{task_id!r} ({role_name}): ${spent_task:.4f} spent + "
                    f"${est:.4f} reserved > ${float(self.budget_per_task_usd):.2f} "
                    "(--budget-per-task); refused before the call, nothing spent",
                    scope="task",
                )
            if (
                self.budget_total_usd is not None
                and self.total_usd + est > float(self.budget_total_usd)
            ):
                raise BudgetExceededError(
                    f"total budget cannot absorb the next call ({role_name}): "
                    f"${self.total_usd:.4f} spent + ${est:.4f} reserved > "
                    f"${float(self.budget_total_usd):.2f} (--budget-total); "
                    "refused before the call, nothing spent",
                    scope="total",
                )
            cap = max_usd if max_usd is not None else self.role_cap_usd(role_name)
            if cap is not None and float(trajectory_spent_usd) + est > float(cap):
                raise RoleCapExceeded(
                    f"role cap cannot absorb the next call for {role_name!r} on "
                    f"task {task_id!r}: ${float(trajectory_spent_usd):.4f} spent "
                    f"in this trajectory + ${est:.4f} reserved > ${float(cap):.2f} "
                    "(max_usd); refused before the call, nothing spent",
                    scope="role",
                )

    # -- charging ---------------------------------------------------------------

    @staticmethod
    def price(
        *, usage: _review_provider_types.Usage, rates: Any, provider_reported_usd: float | None = None
    ) -> float:
        """USD for one attempt: the provider's reported figure when usable,
        else list price at the four rates."""
        reported = _usable_report(provider_reported_usd)
        if reported is not None:
            return reported
        return RateCard.coerce(rates).usd_for(usage)

    def _account(
        self,
        *,
        task_id: str,
        role_name: str,
        usd: float,
        usage: _review_provider_types.Usage,
        elapsed_ms: int,
    ) -> None:
        with self._lock:
            self.total_usd += usd
            self.per_task_usd[task_id] = self.per_task_usd.get(task_id, 0.0) + usd
            self.attempt_count += 1
            role = self.per_role.setdefault(role_name, _fresh_role_reading())
            role["input_tokens"] += usage.input_tokens
            role["output_tokens"] += usage.output_tokens
            role["cache_read_input_tokens"] += usage.cache_read_input_tokens
            role["cache_creation_input_tokens"] += usage.cache_creation_input_tokens
            role["usd"] += usd
            role["attempts"] += 1
            role["elapsed_ms"] += max(0, int(elapsed_ms))

    def _enforce(self, *, task_id: str, role_name: str, model: str) -> None:
        with self._lock:
            if (
                self.budget_per_task_usd is not None
                and self.per_task_usd[task_id] > self.budget_per_task_usd
            ):
                raise BudgetExceededError(
                    f"per-task budget breached for task {task_id!r}: "
                    f"${self.per_task_usd[task_id]:.4f} > "
                    f"${self.budget_per_task_usd:.2f} (--budget-per-task); the "
                    f"last call ({role_name}/{model}) IS recorded in the "
                    "transcript store",
                    scope="task",
                )
            if self.budget_total_usd is not None and self.total_usd > self.budget_total_usd:
                raise BudgetExceededError(
                    f"total budget breached: ${self.total_usd:.4f} > "
                    f"${self.budget_total_usd:.2f} (--budget-total)",
                    scope="total",
                )

    def charge(
        self,
        *,
        task_id: str,
        role_name: str,
        rates: Any,
        usage: _review_provider_types.Usage | None = None,
        provider_reported_usd: float | None = None,
        elapsed_ms: int = 0,
        model: str = "",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        reported_usd: float | None = None,
    ) -> float:
        """Charge one API attempt and return its USD cost.

        Provider-reported cost takes precedence over local rates. The attempt is
        accounted before any budget exception is raised.
        """
        if usage is None:
            usage = _review_provider_types.Usage(
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
            )
        if provider_reported_usd is None:
            provider_reported_usd = reported_usd
        usd = self.price(
            usage=usage, rates=rates, provider_reported_usd=provider_reported_usd
        )
        with self._lock:
            self._account(
                task_id=task_id,
                role_name=role_name,
                usd=usd,
                usage=usage,
                elapsed_ms=elapsed_ms,
            )
            self._enforce(task_id=task_id, role_name=role_name, model=model)
            return usd


class TrajectoryBudget:
    """Track one trajectory's prospective and actual spend.

    `reserve` rejects a breaching turn before transport; `charge` records each paid
    attempt. The trajectory cap raises `RoleCapExceeded`, while task and total caps
    remain on the shared meter. Disabled one-shot roles ignore declared session caps
    unless an explicit override is supplied.
    """

    def __init__(
        self,
        meter: CostMeter,
        *,
        task_id: str,
        role_name: str,
        max_usd: float | None = None,
        enforce_role_cap: bool = True,
    ):
        self.meter = meter
        self.task_id = str(task_id)
        self.role_name = str(role_name)
        self.enforce_role_cap = bool(enforce_role_cap)
        if max_usd is not None:
            self.max_usd: float | None = float(max_usd)
        elif self.enforce_role_cap:
            self.max_usd = meter.role_cap_usd(role_name)
        else:
            self.max_usd = None
        self.spent_usd = 0.0
        self.usage = _review_provider_types.Usage()
        self.attempt_count = 0
        self.elapsed_ms = 0
        #: The usage of the LAST charged attempt: the `last_usage` of the
        #: next turn's `estimate_turn_usd` (the state-machine reserve rule).
        self.last_usage: _review_provider_types.Usage | None = None
        self._durable_reservations: list[str] = []

    @property
    def remaining_usd(self) -> float | None:
        """USD left under this trajectory's own cap (None when uncapped)."""
        if self.max_usd is None:
            return None
        return self.max_usd - self.spent_usd

    def would_exceed(self, est_usd: float) -> bool:
        """True iff `reserve(est_usd)` would refuse (no exception raised)."""
        try:
            # A predicate must not create a durable reservation.  The real
            # call to reserve immediately before transport performs the shared
            # ledger transaction.
            self.meter.reserve(
                task_id=self.task_id,
                role_name=self.role_name,
                est_usd=est_usd,
                trajectory_spent_usd=self.spent_usd,
                max_usd=self.max_usd,
            )
        except BudgetExceededError:
            return True
        return False

    def reserve(self, est_usd: float, *, durable: bool = True) -> str | None:
        """Reserve headroom for the next trajectory turn before transport.

        Check the trajectory cap and shared meter without spending. Raise
        `RoleCapExceeded` for the trajectory or `BudgetExceededError` for task/total
        scope.
        """
        self.meter.reserve(
            task_id=self.task_id,
            role_name=self.role_name,
            est_usd=est_usd,
            trajectory_spent_usd=self.spent_usd,
            max_usd=self.max_usd,
        )
        ledger = self.meter.durable_ledger
        if ledger is not None and durable:
            call_id = (
                f"{self.task_id}:{self.role_name}:"
                f"{uuid.uuid4().hex}"
            )
            try:
                ledger.reserve(
                    self.task_id,
                    self.role_name,
                    max(0.0, float(est_usd))
                    * self.meter.durable_reservation_multiplier,
                    call_id,
                )
            except Exception as exc:  # noqa: BLE001 - normalize shared ledger refusal
                from elt_taskgen.review.budget_ledger import BudgetReservationError

                if isinstance(exc, BudgetReservationError):
                    scope = getattr(exc, "scope", "total")
                    label = "per-task" if scope == "task" else "total"
                    raise BudgetExceededError(
                        f"durable {label} budget cannot absorb the next concurrent "
                        f"call for task {self.task_id!r} ({self.role_name}): {exc}",
                        scope=scope,
                    ) from exc
                raise
            self._durable_reservations.append(call_id)
            return call_id
        return None

    def _commit_durable_reservation(self, actual_usd: float) -> None:
        """Reconcile one successful/charged transport reservation."""

        if not self._durable_reservations:
            return
        call_id = self._durable_reservations.pop(0)
        self.meter.durable_ledger.commit(
            call_id,
            max(0.0, float(actual_usd)),
            task_id=self.task_id,
            role=self.role_name,
        )

    def charge(
        self,
        *,
        usage: _review_provider_types.Usage,
        rates: Any,
        provider_reported_usd: float | None = None,
        elapsed_ms: int = 0,
        model: str = "",
    ) -> float:
        """Charge one API attempt to the meter and this trajectory; a breach
        (task, total, then this trajectory's cap) raises after accounting —
        BudgetExceededError for the task and total budgets, RoleCapExceeded
        for this trajectory's own cap."""
        usd = self.meter.price(
            usage=usage, rates=rates, provider_reported_usd=provider_reported_usd
        )
        with self.meter._lock:
            self.meter._account(
                task_id=self.task_id,
                role_name=self.role_name,
                usd=usd,
                usage=usage,
                elapsed_ms=elapsed_ms,
            )
            self.spent_usd += usd
            self.usage = self.usage + usage
            self.attempt_count += 1
            self.elapsed_ms += max(0, int(elapsed_ms))
            self.last_usage = usage
            self.meter._enforce(
                task_id=self.task_id, role_name=self.role_name, model=model
            )
            if self.max_usd is not None and self.spent_usd > self.max_usd:
                raise RoleCapExceeded(
                    f"role cap breached for {self.role_name!r} on task "
                    f"{self.task_id!r}: ${self.spent_usd:.4f} > "
                    f"${self.max_usd:.2f} (max_usd); the last call IS recorded in "
                    "the transcript store",
                    scope="role",
                )
            return usd
