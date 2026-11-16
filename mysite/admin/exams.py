import uuid

from fastapi import APIRouter, status

from mysite.core.deps import DB, AdminUser, ContentEditor
from mysite.db.schema.exam import (
    ExamFormatIn,
    ExamFormatVersionOut,
    ScoreScaleIn,
    ScoreScaleOut,
    TestAdminOut,
    TestIn,
    ValidationOut,
)
from mysite.db.schema.learning import SkillWeightIn
from mysite.services import exam_admin
from mysite.services import goals as goal_service

router = APIRouter(tags=["admin: exams"])


def _scale_out(scale) -> ScoreScaleOut:
    return ScoreScaleOut(
        id=scale.id,
        format_version_id=scale.format_version_id,
        version=scale.version,
        kind=scale.kind,
        status=scale.status,
        source=scale.source,
    )


# Formats and scales decide every student's score, so only admins may change them.


@router.get("/exam-formats", response_model=list[ExamFormatVersionOut])
async def list_formats(user: ContentEditor, db: DB) -> list:
    return [exam_admin.format_out(fv) for fv in await exam_admin.list_format_versions(db)]


@router.post("/exam-formats", response_model=ExamFormatVersionOut, status_code=status.HTTP_201_CREATED)
async def create_format(data: ExamFormatIn, user: AdminUser, db: DB) -> ExamFormatVersionOut:
    return exam_admin.format_out(await exam_admin.create_format_version(db, data, user))


@router.post("/exam-format-versions/{fv_id}/publish", response_model=ExamFormatVersionOut)
async def publish_format(fv_id: uuid.UUID, user: AdminUser, db: DB) -> ExamFormatVersionOut:
    return exam_admin.format_out(await exam_admin.publish_format_version(db, fv_id, user))


@router.post(
    "/exam-format-versions/{fv_id}/scales", response_model=ScoreScaleOut, status_code=status.HTTP_201_CREATED
)
async def create_scale(fv_id: uuid.UUID, data: ScoreScaleIn, user: AdminUser, db: DB) -> ScoreScaleOut:
    return _scale_out(await exam_admin.create_scale(db, fv_id, data, user))


@router.post("/score-scales/{scale_id}/publish", response_model=ScoreScaleOut)
async def publish_scale(scale_id: uuid.UUID, user: AdminUser, db: DB) -> ScoreScaleOut:
    return _scale_out(await exam_admin.publish_scale(db, scale_id, user))


@router.put("/exam-format-versions/{fv_id}/skill-weights")
async def set_skill_weights(fv_id: uuid.UUID, rows: list[SkillWeightIn], user: AdminUser, db: DB) -> dict:
    """How many questions of each section a skill brings. Drives "where the most points are".
    Until set, weights are estimated from how the published mock exams are built."""
    return {"rows": await goal_service.set_weights(db, fv_id, rows, user)}


# Editors build tests; publishing is an admin decision.


@router.get("/tests", response_model=list[TestAdminOut])
async def list_tests(user: ContentEditor, db: DB) -> list:
    return [exam_admin.test_out(t) for t in await exam_admin.list_tests(db)]


@router.post("/tests", response_model=TestAdminOut, status_code=status.HTTP_201_CREATED)
async def create_test(data: TestIn, user: ContentEditor, db: DB) -> TestAdminOut:
    return exam_admin.test_out(await exam_admin.create_test(db, data, user))


@router.put("/tests/{test_id}", response_model=TestAdminOut)
async def replace_test(test_id: uuid.UUID, data: TestIn, user: ContentEditor, db: DB) -> TestAdminOut:
    return exam_admin.test_out(await exam_admin.replace_test(db, test_id, data, user))


@router.get("/tests/{test_id}/validate", response_model=ValidationOut)
async def validate_test(test_id: uuid.UUID, user: ContentEditor, db: DB) -> ValidationOut:
    problems = await exam_admin.test_problems(db, await exam_admin.get_test(db, test_id))
    return ValidationOut(valid=not problems, problems=problems)


@router.post("/tests/{test_id}/publish", response_model=TestAdminOut)
async def publish_test(test_id: uuid.UUID, user: AdminUser, db: DB) -> TestAdminOut:
    return exam_admin.test_out(await exam_admin.publish_test(db, test_id, user))


@router.post("/tests/{test_id}/retire", response_model=TestAdminOut)
async def retire_test(test_id: uuid.UUID, user: AdminUser, db: DB) -> TestAdminOut:
    return exam_admin.test_out(await exam_admin.retire_test(db, test_id, user))
