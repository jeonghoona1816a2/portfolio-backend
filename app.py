# backend/app.py
import json
import os
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from pydantic import BaseModel, Field, validator
from typing import List, Optional

from db import get_engine

DOTENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=DOTENV_PATH, override=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = get_engine()
    try:
        yield
    finally:
        app.state.engine.dispose()


app = FastAPI(lifespan=lifespan)

origins = [o.strip() for o in (os.getenv("ALLOWED_ORIGINS") or "").split(",") if o.strip()]
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.get("/health")
def health():
    with app.state.engine.connect() as conn:
        return {"ok": conn.execute(text("select 1")).scalar() == 1}


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
                order by id asc
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
