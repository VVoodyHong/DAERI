"""Shared resource types for operator configuration boundaries."""

from typing import Annotated

from pydantic import StringConstraints


ApprovalHookArn = Annotated[str, StringConstraints(pattern=(
    r'^arn:[a-z0-9-]+:lambda:[a-z0-9-]+:[0-9]{12}:function:'
    r'[A-Za-z0-9_-]{1,64}(?::[A-Za-z0-9_-]+|:\$LATEST)?$'))]
