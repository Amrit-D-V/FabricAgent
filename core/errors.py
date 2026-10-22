from __future__ import annotations


class AppError(Exception):
    status = 400


class NotAuthenticated(AppError):
    status = 401


class AccessDenied(AppError):
    status = 403


class NotFound(AppError):
    status = 404


class ServiceUnavailable(AppError):
    status = 503
