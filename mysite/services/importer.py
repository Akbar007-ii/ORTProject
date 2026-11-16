"""Bulk question import from CSV or JSON.

Imported questions always arrive as drafts: they go through the same review chain as
questions typed by hand. Run with dry_run first; it checks every row and writes nothing.

Row fields (CSV columns or JSON keys):
  external_id   unique id of the task in your source (required; re-importing skips it)
  skill_code    e.g. math.quadratic
  language      ky | ru  (the same external_id with another language adds a translation)
  stem          question text, Markdown/LaTeX; images as ![](/media/<id>)
  option_a .. option_d   (JSON may use "options": {"A": ..., "B": ...} instead)
  correct       A | B | C | D
  solution      verified step-by-step solution
  difficulty    1..5 (default 3), estimated_time_sec (optional), source, license
"""

import csv
import hashlib
import io
import json
import secrets
import uuid
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.config import get_settings
from mysite.core.errors import unprocessable
from mysite.db.models import ImportJob, QuestionGroup, QuestionOption, QuestionVersion, Skill, User
from mysite.db.schema.question import QuestionCreateIn
from mysite.services import audit

MAX_ROWS = 2000
KEYS = "ABCDEF"


@dataclass
class Report:
    job_id: uuid.UUID
    dry_run: bool
    total: int
    created: int
    skipped: int
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse(raw: bytes, filename: str) -> list[dict]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise unprocessable("invalid_file", "The file must be UTF-8 text") from exc
    if filename.lower().endswith(".json"):
        try:
            rows = json.loads(text)
        except json.JSONDecodeError as exc:
            raise unprocessable("invalid_file", f"Invalid JSON: {exc.msg} (line {exc.lineno})") from exc
        if isinstance(rows, dict):
            rows = rows.get("questions")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise unprocessable("invalid_file", "JSON must be a list of question objects")
    elif filename.lower().endswith(".csv"):
        rows = list(csv.DictReader(io.StringIO(text)))
    else:
        raise unprocessable("invalid_file", "Upload a .csv or .json file")
    if not rows:
        raise unprocessable("invalid_file", "The file has no rows")
    if len(rows) > MAX_ROWS:
        raise unprocessable("invalid_file", f"At most {MAX_ROWS} rows per file")
    return rows


def _clean(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _options(row: dict) -> dict[str, str]:
    if isinstance(row.get("options"), dict):
        return {str(k).upper(): str(v) for k, v in row["options"].items() if _clean(v)}
    found = {}
    for key in KEYS:
        text = _clean(row.get(f"option_{key.lower()}"))
        if text:
            found[key] = text
    return found


async def run(db: AsyncSession, raw: bytes, filename: str, dry_run: bool, actor: User) -> Report:
    rows = parse(raw, filename)
    file_hash = hashlib.sha256(raw).hexdigest()
    expected_options = get_settings().question_option_count

    skills = {s.code: s for s in (await db.scalars(select(Skill))).all()}
    external_ids = {_clean(r.get("external_id")) for r in rows} - {None}
    groups = {
        g.external_id: g
        for g in (
            await db.scalars(select(QuestionGroup).where(QuestionGroup.external_id.in_(external_ids)))
        ).all()
    }
    existing_languages: dict[uuid.UUID, set[str]] = {}
    if groups:
        for group_id, language in (
            await db.execute(
                select(QuestionVersion.group_id, QuestionVersion.language).where(
                    QuestionVersion.group_id.in_([g.id for g in groups.values()])
                )
            )
        ).all():
            existing_languages.setdefault(group_id, set()).add(language)

    errors: list[dict] = []
    planned: list[tuple[str, Skill, QuestionCreateIn]] = []
    seen: set[tuple[str, str]] = set()
    skipped = 0
    for number, row in enumerate(rows, start=2 if filename.lower().endswith(".csv") else 1):

        def fail(message: str, number=number, row=row) -> None:
            errors.append({"row": number, "external_id": _clean(row.get("external_id")), "error": message})

        external_id = _clean(row.get("external_id"))
        language = (_clean(row.get("language")) or "").lower()
        if not external_id or len(external_id) > 64:
            fail("external_id is required (at most 64 characters)")
            continue
        if (external_id, language) in seen:
            fail("the same external_id and language appear twice in the file")
            continue
        seen.add((external_id, language))

        skill = skills.get(_clean(row.get("skill_code")) or "")
        if skill is None:
            fail(f"unknown skill_code: {row.get('skill_code')!r}")
            continue
        group = groups.get(external_id)
        if group is not None and group.skill_id != skill.id:
            fail("this external_id was imported before with another skill")
            continue
        if group is not None and language in existing_languages.get(group.id, set()):
            skipped += 1  # already imported: running the same file twice changes nothing
            continue

        options = _options(row)
        correct = (_clean(row.get("correct")) or "").upper()
        problems = []
        if len(options) != expected_options:
            problems.append(f"exactly {expected_options} options are required")
        if correct not in options:
            problems.append("correct must name one of the options")
        if not _clean(row.get("solution")):
            problems.append("solution is required")
        if problems:
            fail("; ".join(problems))
            continue
        try:
            payload = QuestionCreateIn(
                skill_id=skill.id,
                language=language,
                stem=_clean(row.get("stem")) or "",
                options=[
                    {"key": k, "text": v, "is_correct": k == correct} for k, v in sorted(options.items())
                ],
                solution=_clean(row.get("solution")),
                difficulty=int(_clean(row.get("difficulty")) or 3),
                estimated_time_sec=int(row["estimated_time_sec"])
                if _clean(row.get("estimated_time_sec"))
                else None,
                source=_clean(row.get("source")),
                license=_clean(row.get("license")),
            )
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                message = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}" for e in exc.errors()
                )
            else:
                message = "difficulty and estimated_time_sec must be numbers"
            fail(message)
            continue
        planned.append((external_id, skill, payload))

    job = ImportJob(
        id=uuid.uuid4(),
        file_hash=file_hash,
        dry_run=dry_run,
        status="failed" if errors else "ok",
        total=len(rows),
        skipped=skipped,
        errors=errors[:500],
        actor_id=actor.id,
        note=filename[:200],
    )
    created = 0
    # All or nothing: a file with any bad row imports nothing, so it can simply be fixed and re-sent.
    if not dry_run and not errors:
        for external_id, skill, payload in planned:
            group = groups.get(external_id)
            if group is None:
                group = QuestionGroup(
                    id=uuid.uuid4(),
                    code=f"Q-{secrets.token_hex(4).upper()}",
                    external_id=external_id,
                    skill_id=skill.id,
                    created_by=actor.id,
                )
                db.add(group)
                await db.flush()
                groups[external_id] = group
            version = QuestionVersion(
                id=uuid.uuid4(),
                group_id=group.id,
                version=1,
                language=payload.language,
                stem=payload.stem,
                solution=payload.solution,
                difficulty=payload.difficulty,
                estimated_time_sec=payload.estimated_time_sec,
                source=payload.source,
                license=payload.license,
                author_id=actor.id,
            )
            db.add(version)
            await db.flush()
            db.add_all(
                QuestionOption(
                    question_version_id=version.id, key=o.key, text=o.text, is_correct=o.is_correct, order=i
                )
                for i, o in enumerate(payload.options)
            )
            created += 1
        audit.record(
            db, actor.id, "questions.import", "import_job", job.id, {"created": created, "skipped": skipped}
        )
    job.created = created
    db.add(job)
    await db.commit()
    return Report(job.id, dry_run, len(rows), created if not dry_run else len(planned), skipped, errors)
