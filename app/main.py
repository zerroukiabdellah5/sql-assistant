# ============================================================
# APP — THIN ROUTES
# ============================================================
# Routes delegate to production modules. No business logic here.
# ============================================================

import os

from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app import approvals, history, uploads
from app.config import (
    DATABASE_PATH,
    GEMINI_API_KEY,
    INDEX_PATH,
    MAX_PROMPT_CHARS,
    NVIDIA_API_KEY,
    NVIDIA_MODEL,
)
from app.db import execute_sql
from app.llm import ask_active, get_provider
from app.reports import build_report_pdf
from app.schema import get_schema


@asynccontextmanager
async def lifespan(app):

    # Vercel Functions expose a read-only filesystem, so the runtime
    # metadata store and upload directory may not be creatable there.
    # Startup must not crash: the routes that need them report the
    # failure when they are actually used.
    try:

        history.init_meta_db()

    except Exception as exc:

        print("META DB INIT SKIPPED:", exc)

    try:

        if not os.path.exists(uploads._upload_root()):
            os.makedirs(uploads._upload_root(), exist_ok=True)

    except Exception as exc:

        print("UPLOAD DIR INIT SKIPPED:", exc)

    yield


app = FastAPI(
    title="Natural Language to SQL AI Assistant",
    version="4.0",
    lifespan=lifespan,
)


# ============================================================
# REQUEST MODELS
# ============================================================

class HistoryTurn(BaseModel):
    prompt: str
    sql: str = ""


class QueryRequest(BaseModel):
    prompt: str
    history: List[HistoryTurn] = Field(default_factory=list)
    session_id: Optional[str] = None


class SessionCreate(BaseModel):
    title: str = "New session"


class ApprovalCreate(BaseModel):
    kind: str
    sql: str
    context: str = ""


class ReportRequest(BaseModel):
    title: str = "SQL Report"
    question: str = ""
    sql: str = ""
    explanation: str = ""
    data: List[dict] = Field(default_factory=list)
    count: int = 0
    truncated: bool = False


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():

    return FileResponse(
        INDEX_PATH
    )


# ============================================================
# HEALTH / VERSION / SCHEMA
# ============================================================

@app.get("/health")
def health():

    return {
        "status": "ok",
        "database_exists": os.path.exists(
            DATABASE_PATH
        )
    }


@app.get("/api/version")
def version():

    return {
        "version": "4.0",
        "backend": "PROD",
        "provider": get_provider().name,
        "database": DATABASE_PATH,
        "database_exists": os.path.exists(
            DATABASE_PATH
        ),
        "gemini_configured": bool(GEMINI_API_KEY),
        "nvidia_configured": bool(
            NVIDIA_API_KEY and NVIDIA_MODEL
        )
    }


@app.get("/api/schema")
def schema():

    try:

        return {
            "schema": get_schema()
        }

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


# ============================================================
# MAIN AI ENDPOINT
# ============================================================

@app.post("/api/ask")
def ask_ai(request: QueryRequest):

    prompt = request.prompt.strip()

    if not prompt:

        raise HTTPException(
            status_code=400,
            detail="Prompt cannot be empty."
        )

    if len(prompt) > MAX_PROMPT_CHARS:

        raise HTTPException(
            status_code=400,
            detail=(
                f"Prompt is too long "
                f"(max {MAX_PROMPT_CHARS} characters)."
            )
        )

    session_id = request.session_id

    if session_id and not history.session_exists(session_id):

        raise HTTPException(
            status_code=404,
            detail="Session not found."
        )

    try:

        if session_id:

            stored_turns = history.build_history_turns(session_id)
            turns = [
                HistoryTurn(prompt=turn["prompt"], sql=turn["sql"])
                for turn in stored_turns
            ]

        else:

            turns = request.history

        generated_sql, explanation = ask_active(
            prompt,
            turns
        )

        data, truncated = execute_sql(
            generated_sql
        )

        if session_id:

            history.add_message(
                session_id, "user",
                prompt=prompt,
            )
            history.add_message(
                session_id, "assistant",
                sql=generated_sql,
                explanation=explanation,
                count=len(data),
                truncated=truncated,
            )

        return {
            "success": True,
            "prompt": prompt,
            "sql": generated_sql,
            "explanation": explanation,
            "data": data,
            "count": len(data),
            "truncated": truncated
        }

    except HTTPException:

        raise

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


# ============================================================
# SESSIONS
# ============================================================

@app.get("/api/sessions")
def list_sessions():

    return {
        "sessions": history.list_sessions()
    }


@app.post("/api/sessions")
def create_session(request: SessionCreate):

    return history.create_session(request.title.strip() or "New session")


@app.get("/api/sessions/{session_id}")
def session_detail(session_id: str):

    session = history.get_session(session_id)

    if session is None:

        raise HTTPException(
            status_code=404,
            detail="Session not found."
        )

    return {
        "session": session,
        "messages": history.get_messages(session_id),
    }


@app.delete("/api/sessions/{session_id}")
def delete_session_route(session_id: str):

    if not history.session_exists(session_id):

        raise HTTPException(
            status_code=404,
            detail="Session not found."
        )

    history.delete_session(session_id)

    return {"deleted": session_id}


# ============================================================
# APPROVALS
# ============================================================

@app.get("/api/approvals")
def list_approvals(status: Optional[str] = None):

    if status and status not in (
        approvals.PENDING,
        approvals.APPROVED,
        approvals.REJECTED,
    ):

        raise HTTPException(
            status_code=400,
            detail="Invalid approval status."
        )

    return {
        "approvals": approvals.list_approvals(status=status)
    }


@app.post("/api/approvals")
def create_approval(request: ApprovalCreate):

    if not request.kind.strip() or not request.sql.strip():

        raise HTTPException(
            status_code=400,
            detail="kind and sql are required."
        )

    return approvals.create_approval(
        kind=request.kind.strip(),
        sql=request.sql.strip(),
        context=request.context,
    )


def _decide_approval(approval_id, action):

    if approvals.get_approval(approval_id) is None:

        raise HTTPException(
            status_code=404,
            detail="Approval not found."
        )

    if not action(approval_id):

        raise HTTPException(
            status_code=500,
            detail="Could not update the approval."
        )

    return approvals.get_approval(approval_id)


@app.post("/api/approvals/{approval_id}/approve")
def approve_approval_route(approval_id: str):

    return _decide_approval(approval_id, approvals.approve_approval)


@app.post("/api/approvals/{approval_id}/reject")
def reject_approval_route(approval_id: str):

    return _decide_approval(approval_id, approvals.reject_approval)


# ============================================================
# UPLOADS (SANDBOXED ALWAYS)
# ============================================================

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):

    if not file.filename:

        raise HTTPException(
            status_code=400,
            detail="No file provided."
        )

    content = await file.read()

    try:

        return uploads.import_file(file.filename, content)

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc)
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=str(exc)
        )


@app.get("/api/imports")
def list_imports():

    return {
        "imports": uploads.list_imports()
    }


@app.get("/api/imports/{import_id}")
def import_detail(import_id: str):

    imported = uploads.get_import(import_id)

    if imported is None:

        raise HTTPException(
            status_code=404,
            detail="Import not found."
        )

    return imported


@app.delete("/api/imports/{import_id}")
def delete_import_route(import_id: str):

    if not uploads.delete_import(import_id):

        raise HTTPException(
            status_code=404,
            detail="Import not found."
        )

    return {"deleted": import_id}


# ============================================================
# REPORTS
# ============================================================

@app.post("/api/report")
def generate_report(request: ReportRequest):

    try:

        path = build_report_pdf(
            title=request.title,
            question=request.question,
            sql=request.sql,
            explanation=request.explanation,
            data=request.data,
            count=request.count,
            truncated=request.truncated,
            schema_text=get_schema(),
        )

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail=f"Report generation failed: {exc}"
        )

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=os.path.basename(path),
    )


# ============================================================
# START SERVER
# ============================================================

def run():

    import uvicorn

    provider = get_provider()
    nvidia_ready = bool(NVIDIA_API_KEY and NVIDIA_MODEL)
    gemini_ready = bool(GEMINI_API_KEY)
    llm_ready = nvidia_ready if provider.name == "nvidia" else gemini_ready

    print("=" * 70)
    print("STARTING PRODUCTION APP")
    print("VERSION: 4.0")
    print("=" * 70)
    print("DATABASE:", DATABASE_PATH)
    print("DATABASE EXISTS:", os.path.exists(DATABASE_PATH))
    print("LLM PROVIDER:", provider.name)
    print("LLM API:", "CONFIGURED" if llm_ready else "NOT CONFIGURED")
    print("=" * 70)

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        reload=False
    )


if __name__ == "__main__":
    run()