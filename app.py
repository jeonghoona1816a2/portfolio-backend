# backend/app.py
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from contextlib import asynccontextmanager
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from pydantic import BaseModel, Field, validator

from db import get_engine

# .env 로드
DOTENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=DOTENV_PATH, override=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 앱 시작할 때 DB 엔진 만들어서 state에 넣기
    app.state.engine = get_engine()
    try:
        yield
    finally:
        # 앱 종료 시 커넥션 정리
        app.state.engine.dispose()


app = FastAPI(lifespan=lifespan)

# CORS 설정
origins = [o.strip() for o in (os.getenv("ALLOWED_ORIGINS") or "").split(",") if o.strip()]
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ─────────────────────────────
# 1. Render가 보는 헬스체크 (가벼움)
# ─────────────────────────────
@app.get("/healthz")
def healthz():
    # 여기서는 DB까지 안 보고 "서버 살아있다" 만 알려줌
    return {"ok": True}


# ─────────────────────────────
# 2. 우리가 보는 헬스체크 (DB까지 확인)
# ─────────────────────────────
@app.get("/health")
def health():
    # DB 연결 확인
    with app.state.engine.connect() as conn:
        ok = conn.execute(text("select 1")).scalar() == 1
        return {"ok": ok}


# ─────────────────────────────
#  프로젝트 목록 조회
# ─────────────────────────────
@app.get("/projects")
def list_projects():
    with app.state.engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                select
                  id,
                  title,
                  description,
                  detail,
                  skills,
                  images,
                  links
                from public.projects
                order by id desc
                """
            )
        ).mappings().all()
        return {"items": [_convert_row(row) for row in rows]}


def _ensure_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _convert_row(row) -> dict:
    data = dict(row)
    for key in ("skills", "images", "links"):
        value = data.get(key)
        if isinstance(value, str):
            try:
                data[key] = json.loads(value)
            except json.JSONDecodeError:
                data[key] = _ensure_list(value)
        else:
            data[key] = _ensure_list(value)
    return data


class ProjectPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default=None)
    detail: Optional[str] = Field(default=None)
    skills: List[str] = Field(default_factory=list)
    images: List[str] = Field(default_factory=list)
    links: List[str] = Field(default_factory=list)

    @validator("description", "detail", pre=True, always=True)
    def _strip_optional(cls, value):
        if value is None:
            return None
        text_value = str(value).strip()
        return text_value or None

    @validator("skills", "images", "links", pre=True, always=True)
    def _normalize_list(cls, value):
        items = _ensure_list(value)
        seen = set()
        normalized = []
        for item in items:
            lowered = item.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized.append(item)
        return normalized


def _project_params(payload: ProjectPayload) -> dict:
    return {
        "title": payload.title.strip(),
        "description": payload.description,
        "detail": payload.detail,
        "skills": json.dumps(payload.skills, ensure_ascii=False),
        "images": json.dumps(payload.images, ensure_ascii=False),
        "links": json.dumps(payload.links, ensure_ascii=False),
    }


@app.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(project: ProjectPayload):
    params = _project_params(project)
    with app.state.engine.begin() as conn:
        row = conn.execute(
            text(
                """
                insert into public.projects (title, description, detail, skills, images, links)
                values (:title, :description, :detail, CAST(:skills AS jsonb), CAST(:images AS jsonb), CAST(:links AS jsonb))
                returning id, title, description, detail, skills, images, links
                """
            ),
            params,
        ).mappings().first()
    return _convert_row(row)


@app.put("/projects/{project_id}")
def update_project(project_id: int, project: ProjectPayload):
    params = _project_params(project)
    params["project_id"] = project_id
    with app.state.engine.begin() as conn:
        row = conn.execute(
            text(
                """
                update public.projects
                  set title = :title,
                      description = :description,
                      detail = :detail,
                      skills = CAST(:skills AS jsonb),
                      images = CAST(:images AS jsonb),
                      links = CAST(:links AS jsonb)
                where id = :project_id
                returning id, title, description, detail, skills, images, links
                """
            ),
            params,
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return _convert_row(row)


@app.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: int):
    with app.state.engine.begin() as conn:
        result = conn.execute(
            text("delete from public.projects where id = :project_id"),
            {"project_id": project_id},
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


class AIChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    task: str = Field(default="qa", max_length=32)
    model: Optional[str] = Field(default=None, max_length=128)


def _ai_system_prompt(task: str) -> str:
    task = (task or "qa").strip().lower()
    if task == "summary":
        return "아래 내용을 한국어로 핵심만 5줄 이내로 요약해줘."
    if task == "translate":
        return "아래 내용을 자연스럽고 정확하게 한국어로 번역해줘."
    if task == "tags":
        return "아래 내용에서 핵심 키워드 8개를 뽑아서 콤마(,)로만 출력해줘."
    return "너는 포트폴리오 사이트의 AI 어시스턴트다. 한국어로 간결하게 답해줘."


def _post_json(url: str, payload: dict, headers: Optional[dict] = None, timeout_sec: int = 60):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, data=body, headers=req_headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        raw = response.read()
        return json.loads(raw.decode("utf-8"))


def _call_ollama(prompt: str, system: str, model: str, base_url: str) -> str:
    url = f"{base_url.rstrip('/')}/api/chat"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    data = _post_json(url, {"model": model, "messages": messages, "stream": False}, timeout_sec=90)
    message = data.get("message") or {}
    return (message.get("content") or data.get("response") or "").strip()


def _call_openai(prompt: str, system: str, model: str, api_key: str) -> str:
    url = "https://api.openai.com/v1/chat/completions"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    data = _post_json(
        url,
        {"model": model, "messages": messages, "temperature": 0.2},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout_sec=90,
    )
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return (message.get("content") or "").strip()


@app.post("/ai/chat")
def ai_chat(payload: AIChatRequest):
    prompt = payload.prompt.strip()
    task = (payload.task or "qa").strip().lower()
    system = _ai_system_prompt(task)

    provider = (os.getenv("AI_PROVIDER") or "").strip().lower()
    openai_api_key = (os.getenv("OPENAI_API_KEY") or "").strip()

    if provider not in ("openai", "ollama"):
        provider = "openai" if openai_api_key else "ollama"

    if provider == "openai":
        if not openai_api_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="OPENAI_API_KEY is not set. Configure env vars or use AI_PROVIDER=ollama.",
            )
        model = (payload.model or os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
        try:
            output = _call_openai(prompt, system, model, openai_api_key)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="ignore")[:800]
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from error
        except urllib.error.URLError as error:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)) from error
    else:
        base_url = (os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").strip()
        model = (payload.model or os.getenv("OLLAMA_MODEL") or "llama3.2").strip()
        try:
            output = _call_ollama(prompt, system, model, base_url)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="ignore")[:800]
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from error
        except urllib.error.URLError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Ollama is not reachable. Start Ollama or configure AI_PROVIDER=openai.",
            ) from error

    if not output:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Empty AI response.")

    return {"output": output, "provider": provider, "model": model, "task": task}
