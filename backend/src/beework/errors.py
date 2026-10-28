"""Business error type shared by API and service modules (re-exported from ``api.conventions``)."""


class ApiError(Exception):
    """Business error rendered to clients as ``{"detail": ..., "code": ...}``."""

    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
