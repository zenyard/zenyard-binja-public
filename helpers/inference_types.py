from __future__ import annotations

from ..zenyard_client.models import (
    Function,
    FunctionOverview,
    GlobalVariable,
    Name,
    NotSwift,
    ParameterType,
    ParametersMapping,
    ReturnType,
    Section,
    StructDefinition,
    SwiftFunction,
    Thunk,
    VariablesMapping,
)

InferenceItem = (
    FunctionOverview
    | Name
    | NotSwift
    | ParametersMapping
    | ParameterType
    | ReturnType
    | StructDefinition
    | SwiftFunction
    | VariablesMapping
)

BvObject = Function | GlobalVariable | Section | Thunk


class _EndOfStream:
    """Marker the download task puts on the inference channel when a cycle
    finishes producing, so the apply task settles analysis + releases the hold
    exactly at stream end (no time-based guessing)."""


END_OF_STREAM = _EndOfStream()

# One applied page (items + its end cursor), or the end-of-stream marker.
InferencePage = tuple[list[InferenceItem], int]
ChannelItem = InferencePage | _EndOfStream
