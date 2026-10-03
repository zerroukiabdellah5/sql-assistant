# ============================================================
# VERCEL SERVERLESS ASGI ENTRYPOINT
# ============================================================
# Vercel's Python runtime discovers this file by convention: a
# module at api/index.py that exposes an ASGI application as
# `app`. Every request is routed here by vercel.json.
#
# It re-exports the one and only FastAPI application in this
# project, app.main.app. Nothing is constructed here: there is no
# second FastAPI instance, no duplicated route, no duplicated
# middleware and no second configuration system. The object below
# is the same object `python main.py` serves locally, so local and
# deployed behaviour cannot drift apart.
#
# The import deliberately happens at module scope, because the
# serverless runtime imports this module to serve a request. That is
# also what makes app.observability.configure_logging() run on
# import and attach the stdout/stderr handler the request and error
# lines need in order to be visible in the platform log at all.
#
# Do not add logic here. Anything that belongs in the application
# belongs in app/, where it is covered by the test suite.
# ============================================================

from app.main import app

__all__ = ["app"]