"""Shared structured errors used by cloud use cases and external boundaries."""

class StoreError(Exception):
    def __init__(self, code: str, message: str, *, task_id: str | None = None,
                 turn_id: str | None = None):
        self.code, self.message = code, message
        self.task_id, self.turn_id = task_id, turn_id
        super().__init__(f"{code}: {message}")
