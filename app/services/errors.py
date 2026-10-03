"""Errors raised by services and dependencies; main.py turns them into JSON responses
of the form {"detail": "<human-readable message>", "code": "<machine-readable code>"}."""


class ServiceError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, detail: str, *, code: str | None = None, headers=None) -> None:
        super().__init__(detail)
        self.detail = detail
        if code:
            self.code = code
        self.headers = headers


class Unauthorized(ServiceError):
    status_code = 401
    code = "not_authenticated"

    def __init__(self, detail: str = "Please sign in.", *, code: str | None = None) -> None:
        super().__init__(detail, code=code, headers={"WWW-Authenticate": "Bearer"})


class Forbidden(ServiceError):
    status_code = 403
    code = "forbidden"


class NotFound(ServiceError):
    status_code = 404
    code = "not_found"


class Conflict(ServiceError):
    status_code = 409
    code = "conflict"


class InvalidInput(ServiceError):
    status_code = 422
    code = "invalid_input"


class Locked(ServiceError):
    status_code = 423
    code = "account_locked"


class PayloadTooLarge(ServiceError):
    status_code = 413
    code = "upload_too_large"


class UnsupportedMediaType(ServiceError):
    status_code = 415
    code = "unsupported_media_type"
