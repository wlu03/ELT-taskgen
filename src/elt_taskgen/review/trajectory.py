"""Hash and verify bounded-session trajectories.

The initial hash binds task, role, tools, policy, and chain version; each turn hashes
stable chain fields in order. Timing, cost, replay status, usage, and admission
provenance remain recorded but unchained. Verification detects edited, dropped,
reordered, or differently bound turns.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from elt_taskgen.models import canonical_json, sha256_hex

__all__ = [
    "CHAIN_VERSION",
    "ChainError",
    "ROUTE_CHAIN_KEYS",
    "TRAJECTORY_RECORD_VERSION",
    "TrajectoryChain",
    "chain_seed",
    "chained_route",
    "to_atif",
    "trajectory_sha256",
    "trial_trajectory_sha256",
    "turn_chain_fields",
    "verify_chain",
    "verify_trajectory_record",
]

#: Version of the chain construction; folded into $$h_0$$ so a later change
#: to the chain fields cannot be confused with a recorded chain. "2": `usage`
#: and `admission` left the chain and `route` is reduced to `chained_route`.
CHAIN_VERSION = "2"

#: Exclude timing, USD, replay provenance, provider cache usage, and admission
#: stamps from evidence hashes; evidence rows record relevant provenance separately.
_UNCHAINED_FIELDS: frozenset[str] = frozenset(
    {"elapsed_model_ms", "elapsed_tool_ms", "usd", "replayed", "usage", "admission"}
)

#: The keys of a turn's `route` block that are chained: the stable identity
#: the turn ran under. `diagnostics_version` (recorded, never compared) and
#: `entry_schema` (a storage format) are not evidence about the turn.
ROUTE_CHAIN_KEYS: tuple[str, ...] = (
    "provider",
    "model",
    "max_tokens",
    "effort",
    "behavior_sha256",
    "tools_sha256",
    "policy_sha256",
)


def chained_route(route: Any) -> dict:
    """The chained projection of a turn's `route` block: `ROUTE_CHAIN_KEYS`
    only, present keys in that order (an empty mapping stays empty)."""
    if not isinstance(route, Mapping):
        return {}
    return {key: _plain(route[key]) for key in ROUTE_CHAIN_KEYS if key in route}


class ChainError(ValueError):
    """A stored chain disagrees with the turns it claims to bind."""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        # An Enum member serialises by value.
        return _plain(value.value)
    return value


def turn_chain_fields(turn: Any) -> dict:
    """The canonical, hashable form of one turn: a `TurnRecord`'s
    `chain_fields()` when it has one, else the mapping (or dataclass fields)
    as given, minus the unchained timing fields, keys sorted by canonical
    JSON."""
    fields_of = getattr(turn, "chain_fields", None)
    if callable(fields_of):
        data = fields_of()
    elif isinstance(turn, Mapping):
        data = dict(turn)
    elif hasattr(turn, "__dataclass_fields__"):
        data = {name: getattr(turn, name) for name in turn.__dataclass_fields__}
    else:
        raise TypeError(f"a turn must be a TurnRecord, a mapping or a dataclass, not {type(turn).__name__}")
    chained = {str(k): _plain(v) for k, v in data.items() if str(k) not in _UNCHAINED_FIELDS}
    if "route" in chained:
        chained["route"] = chained_route(chained["route"])
    return chained


def chain_seed(
    *,
    task_content_hash: str,
    tools_sha256: str,
    policy_sha256: str,
    role: str = "",
) -> str:
    """$$h_0$$: the identity every turn of the session ran under."""
    return sha256_hex(
        canonical_json(
            {
                "chain_version": CHAIN_VERSION,
                "task_content_hash": str(task_content_hash),
                "tools_sha256": str(tools_sha256),
                "policy_sha256": str(policy_sha256),
                "role": str(role),
            }
        )
    )


def _link(previous: str, turn: Any) -> str:
    return sha256_hex(previous + "\n" + canonical_json(turn_chain_fields(turn)))


@dataclass
class TrajectoryChain:
    """The chain as it grows: `append` returns $$h_i$$, `digest` is $$h_n$$."""

    task_content_hash: str
    tools_sha256: str
    policy_sha256: str
    role: str = ""
    hashes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.h0 = chain_seed(
            task_content_hash=self.task_content_hash,
            tools_sha256=self.tools_sha256,
            policy_sha256=self.policy_sha256,
            role=self.role,
        )

    @property
    def digest(self) -> str:
        """`trajectory_sha256`: $$h_n$$ ($$h_0$$ for an empty session)."""
        return self.hashes[-1] if self.hashes else self.h0

    def __len__(self) -> int:
        return len(self.hashes)

    def append(self, turn: Any) -> str:
        link = _link(self.digest, turn)
        self.hashes.append(link)
        return link

    def verify(self, turns: Sequence[Any]) -> bool:
        """True iff recomputing the chain over `turns` reproduces every stored
        link, in order, with the same length."""
        try:
            verify_chain(
                turns,
                self.hashes,
                task_content_hash=self.task_content_hash,
                tools_sha256=self.tools_sha256,
                policy_sha256=self.policy_sha256,
                role=self.role,
            )
        except ChainError:
            return False
        return True


def trajectory_sha256(
    turns: Iterable[Any],
    *,
    task_content_hash: str,
    tools_sha256: str,
    policy_sha256: str,
    role: str = "",
) -> str:
    """$$h_n$$ over `turns` under the given identity (a pure function)."""
    chain = TrajectoryChain(
        task_content_hash=task_content_hash,
        tools_sha256=tools_sha256,
        policy_sha256=policy_sha256,
        role=role,
    )
    for turn in turns:
        chain.append(turn)
    return chain.digest


def verify_chain(
    turns: Sequence[Any],
    hashes: Sequence[str],
    *,
    task_content_hash: str,
    tools_sha256: str,
    policy_sha256: str,
    role: str = "",
) -> str:
    """Recompute the chain over `turns` and compare it link by link with
    `hashes`; returns $$h_n$$ or raises `ChainError` naming the first link
    that disagrees (an edited, dropped or re-ordered turn) or a length
    mismatch."""
    if len(turns) != len(hashes):
        raise ChainError(
            f"chain has {len(hashes)} links but {len(turns)} turns were given"
        )
    previous = chain_seed(
        task_content_hash=task_content_hash,
        tools_sha256=tools_sha256,
        policy_sha256=policy_sha256,
        role=role,
    )
    for index, (turn, stored) in enumerate(zip(turns, hashes)):
        previous = _link(previous, turn)
        if previous != str(stored):
            raise ChainError(f"chain link {index} does not bind the turn at that position")
    return previous


def to_atif(
    turns: Sequence[Any],
    *,
    task_content_hash: str,
    tools_sha256: str,
    policy_sha256: str,
    role: str = "",
    terminal: str = "",
) -> dict:
    """STUB of the Agent Trajectory Interchange Format export (S8 §3.1): the
    chain identity, the chained turn fields and the terminal, in a shape a
    later exporter can widen. It is not yet consumed by any trainer; the
    `format` marker says so."""
    chain = TrajectoryChain(
        task_content_hash=task_content_hash,
        tools_sha256=tools_sha256,
        policy_sha256=policy_sha256,
        role=role,
    )
    steps = []
    for turn in turns:
        link = chain.append(turn)
        steps.append({"fields": turn_chain_fields(turn), "h": link})
    return {
        "format": "atif-stub",
        "chain_version": CHAIN_VERSION,
        "role": role,
        "task_content_hash": task_content_hash,
        "tools_sha256": tools_sha256,
        "policy_sha256": policy_sha256,
        "h0": chain.h0,
        "steps": steps,
        "trajectory_sha256": chain.digest,
        "terminal": str(terminal),
    }


# Trial-bound trajectory digest. It binds the turn chain, trial, behavior, and
# opening prompt without reseeding the original chain.
TRAJECTORY_RECORD_VERSION = "elt-trajectory-v5"


def trial_trajectory_sha256(
    session_sha256: str,
    *,
    task_content_hash: str,
    role: str,
    trial_nonce: str = "",
    behavior_sha256: str = "",
    prompt_sha256: str = "",
) -> str:
    """The trial-bound `trajectory_sha256` of one recorded session: sha256 of
    the canonical JSON of the record version, the runner's chain digest
    (`session_sha256`, $$h_n$$), the task content hash, the role, the trial
    nonce ('' outside a metrology trial), the seat's `behavior_sha256` and
    the `prompt_sha256` the session opened on. A pure function."""
    return sha256_hex(
        canonical_json(
            {
                "record_version": TRAJECTORY_RECORD_VERSION,
                "chain_version": CHAIN_VERSION,
                "session_sha256": str(session_sha256),
                "task_content_hash": str(task_content_hash),
                "role": str(role),
                "trial_nonce": str(trial_nonce or ""),
                "behavior_sha256": str(behavior_sha256 or ""),
                "prompt_sha256": str(prompt_sha256 or ""),
            }
        )
    )


def verify_trajectory_record(record: Mapping[str, Any]) -> str:
    """Recompute and validate every hash in a trajectory record.

    Verify the turn chain under its task, tools, policy, and role, then verify the trial
    binding under nonce, behavior, and prompt. Return `trajectory_sha256`; raise
    `ChainError` for any edited, dropped, reordered, or differently bound content.
    """
    if not isinstance(record, Mapping):
        raise ChainError("a trajectory record is a mapping")
    turns = record.get("turns")
    if not isinstance(turns, Sequence) or isinstance(turns, (str, bytes)):
        raise ChainError("trajectory record carries no turns list")
    hashes = record.get("chain_hashes")
    if not isinstance(hashes, Sequence) or isinstance(hashes, (str, bytes)):
        raise ChainError("trajectory record carries no chain_hashes list")
    identity = {
        "task_content_hash": str(record.get("task_content_hash") or ""),
        "tools_sha256": str(record.get("tools_sha256") or ""),
        "policy_sha256": str(record.get("policy_sha256") or ""),
        "role": str(record.get("role") or ""),
    }
    digest = verify_chain(list(turns), [str(h) for h in hashes], **identity)
    if digest != str(record.get("session_sha256") or ""):
        raise ChainError("trajectory record's session_sha256 is not the chain over its turns")
    bound = trial_trajectory_sha256(
        digest,
        task_content_hash=identity["task_content_hash"],
        role=identity["role"],
        trial_nonce=str(record.get("trial_nonce") or ""),
        behavior_sha256=str(record.get("behavior_sha256") or ""),
        prompt_sha256=str(record.get("prompt_sha256") or ""),
    )
    if bound != str(record.get("trajectory_sha256") or ""):
        raise ChainError(
            "trajectory record's trajectory_sha256 does not bind its chain to its "
            "trial, behaviour and prompt"
        )
    return bound
