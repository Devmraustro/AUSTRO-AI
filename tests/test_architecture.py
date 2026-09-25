"""Phase B architecture lock-in tests.

Pure unit / integration tests for the new layered `app/` package: the error
taxonomy, the DI container, the AI gateway (retries, rate limit, fallback,
telemetry, unsupported capabilities), conversation memory, and a repository
round-trip through the application services. No network, no real Telegram API.
"""

import dataclasses

import pytest

from app.ai.gateway import AIGateway
from app.config.settings import settings
from app.core.container import build_container, get_services
from app.core.errors import (
    AIProviderError,
    AuthorizationError,
    ConfigurationError,
    DatabaseError,
    ExternalServiceError,
    NotFoundError,
    RateLimitError,
    ValidationError,
    public_message,
)
from app.core.interfaces import AIProvider
from app.domain.ai import AIRequest
from app.domain.entities import NewGoal, NewHabit, NewPlan
from app.domain.context import UserContext
from app.memory.conversation import ConversationMemory


class _FakeProvider(AIProvider):
    """Provider stub: may be unavailable, fail N times or hit a rate limit."""

    name = "fake"

    def __init__(self, available=True, fail_times=0, raise_rate_limit=False):
        self._available = available
        self.fail_times = fail_times
        self.raise_rate_limit = raise_rate_limit
        self.calls = 0

    model = "fake-model"

    @property
    def is_available(self):
        return self._available

    async def generate(self, request):
        self.calls += 1
        if self.raise_rate_limit:
            raise RateLimitError("daily quota reached")
        if self.calls <= self.fail_times:
            raise AIProviderError("simulated failure")
        return "fake-ok"


class _FakeLocal:
    """Duck-typed local fallback returning a fixed, assertable answer."""

    name = "fake-local"

    def generate(self, request):
        return "local-answer"


def _test_settings(**overrides):
    base = {"gemini_api_key": "fake_key_12345"}
    base.update(overrides)
    return dataclasses.replace(settings, **base)


# ============================ ERROR TAXONOMY ============================


@pytest.mark.parametrize(
    ("error_type", "expected_message"),
    [
        (ValidationError, "❌ البيانات المدخلة غير صحيحة."),
        (AuthorizationError, "⛔ هذه العملية غير مسموح بها."),
        (NotFoundError, "❌ لم يتم العثور على البيانات المطلوبة."),
        (AIProviderError, "🤖 تعذر الوصول إلى الذكاء الاصطناعي حالياً. حاول لاحقاً."),
        (DatabaseError, "❌ حدث خطأ في قاعدة البيانات. حاول مرة أخرى لاحقاً."),
        (ExternalServiceError, "❌ حدث خطأ في خدمة خارجية. حاول لاحقاً."),
        (RateLimitError, "⚠️ وصلت إلى الحد المسموح من الاستخدام اليومي. حاول لاحقاً."),
        (ConfigurationError, "❌ خطأ في الإعدادات. تأكد من ملف .env ثم أعد التشغيل."),
    ],
)
def test_public_message_maps_known_errors(error_type, expected_message):
    assert public_message(error_type()) == expected_message


def test_public_message_never_leaks_raw_internals():
    assert public_message(RuntimeError("secret internal detail")) == \
        "❌ عذراً، حدث خطأ. الرجاء المحاولة مرة أخرى."
    assert "secret internal detail" not in public_message(RuntimeError("secret internal detail"))


def test_public_message_passes_safe_validation_text():
    assert public_message(ValidationError("❌ الرجاء إدخال رقم صحيح:")) == "❌ الرجاء إدخال رقم صحيح:"


def test_public_message_passes_safe_provider_text():
    assert public_message(AIProviderError("سيتم المحاولة لاحقاً")) == "سيتم المحاولة لاحقاً"


# ============================ DI CONTAINER ============================


def test_build_container_wires_every_service():
    container = build_container()
    for name in ("ai", "users", "goals", "plans", "habits", "progress",
                 "reviews", "reminders", "dashboard", "chat", "learning",
                 "coaching", "knowledge", "memory"):
        assert getattr(container, name) is not None, f"{name} not wired"
    assert container.settings is settings
    assert container.ai is not None


class _FakeContext:
    def __init__(self, bot_data):
        self.bot_data = bot_data


def test_get_services_resolves_from_context():
    container = build_container()
    context = _FakeContext(container.bot_data())
    assert get_services(context) is container


def test_get_services_raises_when_missing():
    with pytest.raises(ConfigurationError):
        get_services(_FakeContext({}))


# ============================ AI GATEWAY ============================


async def test_gateway_success_records_telemetry():
    provider = _FakeProvider()
    gateway = AIGateway(settings_=_test_settings(ai_max_retries=2), provider=provider)
    response = await gateway.generate(AIRequest(capability="chat", prompt="hi"))
    assert response.text == "fake-ok"
    assert response.metadata.success is True
    assert response.metadata.provider == "fake"
    assert response.metadata.retry_count == 0
    assert provider.calls == 1


async def test_gateway_retries_then_succeeds():
    provider = _FakeProvider(fail_times=1)
    gateway = AIGateway(settings_=_test_settings(ai_max_retries=2), provider=provider)
    response = await gateway.generate(AIRequest(capability="chat", prompt="hi"))
    assert response.text == "fake-ok"
    assert response.metadata.retry_count == 1
    assert provider.calls == 2


async def test_gateway_rate_limit_does_not_retry_and_falls_back():
    provider = _FakeProvider(raise_rate_limit=True)
    local = _FakeLocal()
    gateway = AIGateway(
        settings_=_test_settings(ai_max_retries=2),
        provider=provider,
        local=local,
    )
    response = await gateway.generate(AIRequest(capability="chat", prompt="hi"))
    assert response.text == "local-answer"
    assert response.metadata.success is True
    assert response.metadata.provider == "fake-local"
    assert provider.calls == 1  # no retry on rate limit


async def test_gateway_falls_back_when_provider_unavailable():
    provider = _FakeProvider(available=False)
    local = _FakeLocal()
    gateway = AIGateway(settings_=_test_settings(), provider=provider, local=local)
    response = await gateway.generate(AIRequest(capability="chat", prompt="hi"))
    assert response.text == "local-answer"
    assert response.metadata.provider == "fake-local"
    assert provider.calls == 0


async def test_gateway_all_fail_returns_safe_message():
    provider = _FakeProvider(fail_times=100)
    gateway = AIGateway(
        settings_=_test_settings(ai_max_retries=0, use_local_fallback=False),
        provider=provider,
    )
    response = await gateway.generate(AIRequest(capability="chat", prompt="hi"))
    assert response.text == "عذراً، الخدمة غير متاحة حالياً. حاول لاحقاً."
    assert response.metadata.success is False
    assert "AIProviderError" in response.metadata.error


async def test_gateway_unsupported_capability_degrades_gracefully():
    gateway = AIGateway(settings_=_test_settings(), provider=_FakeProvider())
    response = await gateway.generate(AIRequest(capability="safety_check", prompt="x"))
    assert response.text == "⚠️ هذه الميزة غير متاحة حالياً."
    assert response.metadata.success is False
    assert response.metadata.provider == "none"


async def test_gateway_local_fallback_never_raises_when_provider_duck_type():
    """A non-AIProvider injected fallback must keep the API working."""
    class _BoomLocal:
        name = "boom-local"

        def generate(self, request):
            raise RuntimeError("fallback blew up")

    gateway = AIGateway(
        settings_=_test_settings(use_local_fallback=True),
        provider=_FakeProvider(available=False),
        local=_BoomLocal(),
    )
    response = await gateway.generate(AIRequest(capability="chat", prompt="hi"))
    assert response.metadata.success is False
    assert response.text == "عذراً، الخدمة غير متاحة حالياً. حاول لاحقاً."


# ============================ CONVERSATION MEMORY ============================


def test_conversation_memory_bounds_and_recent():
    memory = ConversationMemory(exchange_limit=10)
    for i in range(15):
        memory.add(1, f"q{i}", f"a{i}")
    assert len(memory.recent(1, window=100)) == 10
    assert memory.recent(1, window=3) == [
        {"user": "q12", "ai": "a12"},
        {"user": "q13", "ai": "a13"},
        {"user": "q14", "ai": "a14"},
    ]
    memory.reset(1)
    assert memory.recent(1) == []
    memory.add(2, "x", "y")
    memory.clear()
    assert memory.recent(2) == []


def test_conversation_memory_is_per_user():
    memory = ConversationMemory()
    memory.add(1, "a", "b")
    memory.add(2, "c", "d")
    assert memory.recent(1) == [{"user": "a", "ai": "b"}]
    assert memory.recent(2) == [{"user": "c", "ai": "d"}]


# ============================ SERVICES / REPOSITORIES ============================



def test_service_repository_roundtrip():
    """Create user, goal, habit, plan through services; read them back."""
    container = build_container()

    assert container.users.ensure_user(UserContext(user_id=99, username="arch", first_name="A"))
    profile = container.users.profile(99)
    assert profile["user_id"] == 99
    assert container.users.parse_age("25") == 25

    goal_id = container.goals.create(NewGoal(user_id=99, title="تعلّم بايثون", description="بداية", category="study"))
    assert goal_id is not None
    assert any(g["title"] == "تعلّم بايثون" for g in container.goals.list(99))

    habit_id = container.habits.create(NewHabit(user_id=99, name="قراءة", description="20 دقيقة", frequency="daily"))
    assert habit_id is not None
    assert any(h["name"] == "قراءة" for h in container.habits.list(99))

    plan_ok = container.plans.create(
        NewPlan(user_id=99, date="2026-09-19", tasks=[{"task": "درس"}], review_time="20:00")
    )
    assert plan_ok is True
    plan = container.plans.for_date(99, "2026-09-19")
    assert plan is not None
    assert plan["tasks"] == [{"task": "درس"}]

    with pytest.raises(ValidationError):
        container.users.parse_age("xx")
    with pytest.raises(ValidationError):
        container.users.parse_age("150")


# ============================ LEGACY CONFIG SHIM ============================


def test_config_shim_exports_expected_names():
    import config

    assert config.BOT_TOKEN == settings.bot_token
    assert config.LOGS_PATH == settings.logs_path
    assert config.DB_PATH == settings.db_path
    assert config.ARABIC_RESPONSES["welcome"]
    assert config.REMINDER_TIMES["morning"] == "08:00"