"""App errors. Each one carries the HTTP status code it is returned with."""


class AppError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationFailed(AppError):
    status_code = 400


class NotFound(AppError):
    status_code = 404


class Conflict(AppError):
    status_code = 409


class PayloadTooLarge(AppError):
    status_code = 413


class LLMUnavailable(AppError):
    """Ollama is not running, or the requested model is not installed."""
    status_code = 503
