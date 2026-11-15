class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def dialog_not_found() -> AppError:
    return AppError(404, "dialog_not_found", "Dialog not found")


def idempotency_conflict() -> AppError:
    return AppError(409, "idempotency_conflict", "client_message_id was used for another message")
