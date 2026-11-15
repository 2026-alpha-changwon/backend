"""handler의 공통 오류 처리기로 전달하는 업무 오류입니다."""


class ApiError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
