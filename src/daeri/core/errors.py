"""Shared structured errors used by policy, use cases and external boundaries."""

class StoreError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")
