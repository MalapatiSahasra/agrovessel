"""Vercel serverless entrypoint -- re-exports the FastAPI app so Vercel's
Python runtime can serve it as an ASGI function."""

from app.main import app

__all__ = ["app"]
