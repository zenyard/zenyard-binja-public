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
