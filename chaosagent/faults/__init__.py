"""Fault injection: 8 classes, 4 outcome types, 4 schedules."""

from chaosagent.faults.classes import REGISTRY, InjectionContext, get_fault
from chaosagent.faults.injector import FaultInjector, FaultyEnvironment
from chaosagent.faults.schedule import bucket_for, build_schedule, position_index
from chaosagent.faults.types import (
    CONTROL,
    FAULT_CLASSES,
    BlockAndError,
    CallOutcome,
    CorruptResult,
    DelayThenSucceed,
    FaultOutcome,
    FaultSpec,
    InjectionRecord,
    PassThrough,
    PendingCall,
    SuppressResult,
)

__all__ = [
    "CONTROL",
    "FAULT_CLASSES",
    "REGISTRY",
    "BlockAndError",
    "CallOutcome",
    "CorruptResult",
    "DelayThenSucceed",
    "FaultInjector",
    "FaultOutcome",
    "FaultSpec",
    "FaultyEnvironment",
    "InjectionContext",
    "InjectionRecord",
    "PassThrough",
    "PendingCall",
    "SuppressResult",
    "bucket_for",
    "build_schedule",
    "get_fault",
    "position_index",
]
