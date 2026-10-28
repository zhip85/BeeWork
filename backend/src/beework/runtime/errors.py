class RuntimeFailure(Exception):
    """A run failed for a reason the user can be told about; ``code`` is machine readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RunCancelled(Exception):
    pass


class RunTimedOut(RuntimeFailure):
    def __init__(self, seconds: int) -> None:
        super().__init__("runtime.timeout", f"运行超过 {seconds} 秒，已停止")
