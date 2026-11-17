class ServiceError(Exception):
    """Ошибка прикладного уровня с HTTP-статусом и машинным кодом для UI."""

    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}
