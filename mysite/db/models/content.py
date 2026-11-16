import uuid

from sqlalchemy import ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from mysite.db.database import Base


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title_ky: Mapped[str] = mapped_column(String(120))
    title_ru: Mapped[str] = mapped_column(String(120))
    order: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True)


class Topic(Base):
    """Tree inside a subject: a topic with parent_id is a subtopic."""

    __tablename__ = "topics"
    __table_args__ = (Index("ix_topics_subject_id", "subject_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("subjects.id", ondelete="RESTRICT"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("topics.id", ondelete="RESTRICT"))
    title_ky: Mapped[str] = mapped_column(String(160))
    title_ru: Mapped[str] = mapped_column(String(160))
    order: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True)

    skills: Mapped[list["Skill"]] = relationship(back_populates="topic", lazy="selectin")


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (Index("ix_skills_topic_id", "topic_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    topic_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("topics.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(64), unique=True)
    title_ky: Mapped[str] = mapped_column(String(200))
    title_ru: Mapped[str] = mapped_column(String(200))
    order: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(default=True)

    topic: Mapped[Topic] = relationship(back_populates="skills")
