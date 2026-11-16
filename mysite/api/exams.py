import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response, status

from mysite.core.deps import DB, CurrentUser
from mysite.db.models import Locale, TestKind
from mysite.db.schema.catalog import Code
from mysite.db.schema.exam import (
    AnswerIn,
    AnswerOut,
    CheckpointOut,
    ResultOut,
    SessionListItem,
    SessionOut,
    TestOut,
)
from mysite.services import sessions as session_service

router = APIRouter(tags=["exams"])


@router.get("/tests", response_model=list[TestOut])
async def list_tests(
    db: DB,
    kind: TestKind | None = None,
    subject_id: uuid.UUID | None = None,
    language: Locale | None = None,
) -> list[TestOut]:
    """Published tests. `kind=mock` gives the full mock exams."""
    return await session_service.list_tests(db, kind=kind, subject_id=subject_id, language=language)


@router.get("/tests/{test_id}", response_model=TestOut)
async def get_test(test_id: uuid.UUID, db: DB) -> TestOut:
    return await session_service.get_test(db, test_id)


@router.post("/tests/{test_id}/start", response_model=SessionOut)
async def start_test(
    test_id: uuid.UUID,
    user: CurrentUser,
    db: DB,
    response: Response,
    idempotency_key: Annotated[str | None, Header(max_length=64)] = None,
) -> SessionOut:
    """201 for a new session, 200 when an unfinished (or same-key) session is returned instead."""
    session, created = await session_service.start(db, user.id, test_id, idempotency_key)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return session


@router.get("/sessions", response_model=list[SessionListItem])
async def list_sessions(
    user: CurrentUser,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SessionListItem]:
    return await session_service.list_sessions(db, user.id, limit, offset)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: uuid.UUID, user: CurrentUser, db: DB) -> SessionOut:
    """Restores the exam screen after a reload or a new login."""
    return await session_service.get_state(db, user.id, session_id)


@router.put("/sessions/{session_id}/answers/{question_id}", response_model=AnswerOut)
async def save_answer(
    session_id: uuid.UUID, question_id: uuid.UUID, data: AnswerIn, user: CurrentUser, db: DB
) -> AnswerOut:
    """Autosave. Safe to retry and to replay from an offline queue (see `seq`)."""
    return await session_service.answer(db, user.id, session_id, question_id, data)


@router.post("/sessions/{session_id}/sections/{section_code}/close", response_model=SessionOut)
async def close_section(session_id: uuid.UUID, section_code: Code, user: CurrentUser, db: DB) -> SessionOut:
    return await session_service.close_section(db, user.id, session_id, section_code)


@router.post("/sessions/{session_id}/submit", response_model=ResultOut)
async def submit_session(session_id: uuid.UUID, user: CurrentUser, db: DB) -> ResultOut:
    return await session_service.submit(db, user.id, session_id)


@router.get("/sessions/{session_id}/result", response_model=ResultOut)
async def get_result(session_id: uuid.UUID, user: CurrentUser, db: DB) -> ResultOut:
    return await session_service.get_result(db, user.id, session_id)


@router.get("/checkpoints", response_model=list[CheckpointOut])
async def list_checkpoints(user: CurrentUser, db: DB) -> list[CheckpointOut]:
    """The student's mock exam results in time order: the points of the score trajectory."""
    return await session_service.list_checkpoints(db, user.id)
