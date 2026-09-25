"""AUSTRO AI - Learning engine interfaces (Phase C scope).

The full book-to-course learning engine (chapter maps, lesson plans, practice
questions, quizzes, active recall, spaced review, mastery tracking) is a
future phase. This module defines the protocol ONLY - no implementation, no
database - so consumer code (UI, services, tests of the contract) can be built
against a stable interface without inventing behavior prematurely.
"""

from __future__ import annotations

import abc
from typing import List

from app.knowledge.models import ChapterMap, LessonPlan, StudyRecall


class LearningEngine(abc.ABC):
    """Interface for generating learning material from a user's book."""

    @abc.abstractmethod
    def build_chapter_map(self, owner_user_id: int, source_id: int) -> ChapterMap:
        """Return the ordered chapters/sections of a book."""

    @abc.abstractmethod
    def plan_lessons(self, owner_user_id: int, source_id: int,
                     day: int = 1) -> LessonPlan:
        """Plan a day's lessons from a book."""

    @abc.abstractmethod
    def generate_questions(self, owner_user_id: int, source_id: int,
                           count: int = 5) -> List[str]:
        """Generate practice questions from the book's content."""

    @abc.abstractmethod
    def generate_quiz(self, owner_user_id: int, source_id: int,
                      count: int = 5, difficulty: str = "medium") -> dict:
        """Generate a structured quiz from the book's content."""

    @abc.abstractmethod
    def active_recall(self, owner_user_id: int, source_id: int,
                      chunk_row_id: int) -> StudyRecall:
        """Create an active-recall prompt for a section."""

    @abc.abstractmethod
    def spaced_review(self, owner_user_id: int, source_id: int,
                      interval_days: int) -> List[StudyRecall]:
        """Compute which sections are due for review."""

    @abc.abstractmethod
    def mastery(self, owner_user_id: int, source_id: int) -> dict:
        """Return a mastery overview for a book."""


class NotImplementedLearningEngine(LearningEngine):
    """Deterministic placeholder that fails with a clear, safe message."""

    def build_chapter_map(self, owner_user_id: int, source_id: int) -> ChapterMap:
        raise NotImplementedError("ميزة خريطة الفصول قادمة في مرحلة لاحقة")

    def plan_lessons(self, owner_user_id: int, source_id: int, day: int = 1) -> LessonPlan:
        raise NotImplementedError("ميزة تخطيط الدروس قادمة في مرحلة لاحقة")

    def generate_questions(self, owner_user_id: int, source_id: int,
                           count: int = 5) -> List[str]:
        raise NotImplementedError("ميزة الأسئلة قادمة في مرحلة لاحقة")

    def generate_quiz(self, owner_user_id: int, source_id: int,
                      count: int = 5, difficulty: str = "medium") -> dict:
        raise NotImplementedError("ميزة الاختبارات قادمة في مرحلة لاحقة")

    def active_recall(self, owner_user_id: int, source_id: int,
                      chunk_row_id: int) -> StudyRecall:
        raise NotImplementedError("ميزة الاستدعاء النشط قادمة في مرحلة لاحقة")

    def spaced_review(self, owner_user_id: int, source_id: int,
                      interval_days: int) -> List[StudyRecall]:
        raise NotImplementedError("ميزة المراجعة المتباعدة قادمة في مرحلة لاحقة")

    def mastery(self, owner_user_id: int, source_id: int) -> dict:
        raise NotImplementedError("ميزة التتبع قادمة في مرحلة لاحقة")


__all__ = ["LearningEngine", "NotImplementedLearningEngine"]