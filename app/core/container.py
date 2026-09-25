"""
AUSTRO AI - Dependency injection container.

Composition root: builds every service with its real dependencies once, at
application build time. The container lives in `context.bot_data["container"]`
and handlers pull the services they need via `get_services(context)`.

Handlers therefore never import global `db`/`ai_engine` singletons; they talk
to services (which own repositories, AI gateways and business rules).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Optional

from app.ai.gateway import AIGateway
from app.application.chat import ChatService
from app.application.dashboard import DashboardService
from app.application.goals import GoalService
from app.application.habits import HabitService
from app.application.plans import PlanService
from app.application.progress import ProgressService
from app.application.reminders import ReminderService
from app.application.reviews import ReviewService
from app.application.users import UserService
from app.coaching.service import CoachingService
from app.config.settings import Settings, settings as default_settings
from app.core.errors import ConfigurationError
from app.database import db as default_db
from app.database.repositories import Database
from app.knowledge.collections import CollectionService
from app.knowledge.embeddings import EmbeddingService
from app.knowledge.ingestion import IngestionPipeline
from app.knowledge.permissions import KnowledgePermissions
from app.knowledge.rag import RAGService
from app.knowledge.repositories import KnowledgeStore
from app.knowledge.retrieval import RetrievalService
from app.knowledge.service import KnowledgeService
from app.knowledge.storage import StorageService
from app.learning.engine import AdaptiveLearningEngine
from app.learning.repositories import LearningStore
from app.learning.service import LearningService
from app.memory.extractors import CandidateExtractor
from app.memory.repositories import MemoryStore
from app.memory.retrieval import MemoryRetrieval
from app.memory.service import MemoryService
from app.memory.write_gate import MemoryWriteGate

if TYPE_CHECKING:  # pragma: no cover
    from telegram.ext import ContextTypes


@dataclass
class ServiceContainer:
    """Every runtime dependency the presentation layer may request."""

    db: Database
    settings: Settings

    ai: AIGateway = field(default=None)  # type: ignore[assignment]

    users: UserService = field(default=None)  # type: ignore[assignment]
    goals: GoalService = field(default=None)  # type: ignore[assignment]
    plans: PlanService = field(default=None)  # type: ignore[assignment]
    habits: HabitService = field(default=None)  # type: ignore[assignment]
    progress: ProgressService = field(default=None)  # type: ignore[assignment]
    reviews: ReviewService = field(default=None)  # type: ignore[assignment]
    reminders: ReminderService = field(default=None)  # type: ignore[assignment]
    dashboard: DashboardService = field(default=None)  # type: ignore[assignment]
    chat: ChatService = field(default=None)  # type: ignore[assignment]
    learning: LearningService = field(default=None)  # type: ignore[assignment]
    learning_engine: AdaptiveLearningEngine = field(default=None)  # type: ignore[assignment]
    coaching: CoachingService = field(default=None)  # type: ignore[assignment]
    knowledge: KnowledgeService = field(default=None)  # type: ignore[assignment]
    memory: MemoryService = field(default=None)  # type: ignore[assignment]

    def bot_data(self) -> Dict[str, Any]:
        """The dict that should be attached to `context.bot_data`."""
        return {"container": self}


def build_container(
    db_instance: Optional[Database] = None,
    settings_instance: Optional[Settings] = None,
) -> ServiceContainer:
    """Compose the full service graph (composition root)."""
    database = db_instance or default_db
    config = settings_instance or default_settings

    ai_gateway = AIGateway(settings_=config)
    users = UserService(database)
    goals = GoalService(database)
    plans = PlanService(database)
    habits = HabitService(database)
    progress = ProgressService(database)
    reviews = ReviewService(database)
    reminders = ReminderService(database)
    dashboard = DashboardService(database)

    memory_store = MemoryStore(database._manager)
    memory_gate = MemoryWriteGate(memory_store)
    memory_retrieval = MemoryRetrieval(memory_store, config)
    memory = MemoryService(
        store=memory_store,
        gate=memory_gate,
        extractor=CandidateExtractor(),
        retrieval=memory_retrieval,
        settings_=config,
    )

    chat = ChatService(ai=ai_gateway, memory_service=memory)
    learning = LearningService(ai_gateway)
    coaching = CoachingService(ai_gateway)

    learning_store = LearningStore(database._manager)

    knowledge_store = KnowledgeStore(database._manager)
    storage = StorageService(config.knowledge_storage_path, knowledge_store)
    embeddings = EmbeddingService(config)
    retrieval = RetrievalService(knowledge_store, embeddings, config)
    rag = RAGService(retrieval, ai_gateway, config)
    collections = CollectionService(knowledge_store)
    permissions = KnowledgePermissions(knowledge_store)
    pipeline = IngestionPipeline(
        knowledge_store, storage, embeddings=embeddings, settings_=config
    )
    knowledge = KnowledgeService(
        store=knowledge_store,
        storage=storage,
        settings_=config,
        embeddings=embeddings,
        retrieval=retrieval,
        rag=rag,
        collections=collections,
        permissions=permissions,
        pipeline=pipeline,
    )

    learning_engine = AdaptiveLearningEngine(
        store=learning_store,
        ai=ai_gateway,
        settings={
            "learning_use_llm": config.learning_use_llm,
            "learning_default_session_minutes": config.learning_default_session_minutes,
            "learning_review_minutes": config.learning_review_minutes,
            "learning_planner_max_items": config.learning_planner_max_items,
        },
        knowledge=knowledge_store,
        memory_service=memory,
    )

    return ServiceContainer(
        db=database,
        settings=config,
        ai=ai_gateway,
        users=users,
        goals=goals,
        plans=plans,
        habits=habits,
        progress=progress,
        reviews=reviews,
        reminders=reminders,
        dashboard=dashboard,
        chat=chat,
        learning=learning,
        learning_engine=learning_engine,
        coaching=coaching,
        knowledge=knowledge,
        memory=memory,
    )


def get_services(context: "ContextTypes.DEFAULT_TYPE") -> ServiceContainer:
    """Resolve the service container from a Telegram context."""
    container = context.bot_data.get("container")
    if container is None:
        raise ConfigurationError("DI container is not configured in bot_data")
    return container