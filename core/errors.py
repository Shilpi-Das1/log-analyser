"""Application exceptions. `status` is the HTTP status the web layer returns."""


class AppError(Exception):
    status = 400


class LLMError(AppError):
    status = 502


class GitHubError(AppError):
    status = 502


class PatchError(AppError):
    status = 422
