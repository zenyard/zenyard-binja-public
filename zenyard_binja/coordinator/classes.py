import typing as ty

from dataclasses import dataclass


@dataclass(frozen=True)
class UserAction:
    kind: ty.Literal["create_revision", "check_inferences", "ensure_setup"]
