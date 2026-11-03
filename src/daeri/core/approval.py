"""Current plan and host-confirmed user decisions, independent of transport."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanApproval:
    tool_use_id: str
    plan: str
    decision: str = 'pending'
    response: str = ''


@dataclass(frozen=True)
class ApprovalResponse:
    decision: str
    response: str = ''
