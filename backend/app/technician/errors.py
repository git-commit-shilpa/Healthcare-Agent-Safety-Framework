"""Task API errors: a safe human message plus a machine-readable code.

Responses look like {"detail": "<safe message>", "code": "<machine reason>"}. The
detail never contains database messages, SQL details, notes or identifiers.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class TaskApiError(Exception):
    def __init__(self, status_code: int, detail: str, code: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


def register_error_handlers(application: FastAPI) -> None:
    @application.exception_handler(TaskApiError)
    async def _handle_task_api_error(_request: Request, error: TaskApiError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": error.detail, "code": error.code},
        )
