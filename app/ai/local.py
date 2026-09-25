"""
AUSTRO AI - Local fallback provider.

Deterministic, offline responses used whenever the cloud provider is
unavailable (no key, network down, quota/rate limits, timeouts). Every canned
block is a faithful copy of the original local fallback texts.
"""

from __future__ import annotations

import random
from typing import Any, Dict

from app.config.prompts import LOCAL_AI_RESPONSES
from app.core.errors import AIProviderError
from app.domain.ai import AIRequest

_GREETING_KEYWORDS = {
    "greeting": ["مرحبا", "أهلا", "سلام", "hello", "hi", "صباح", "مساء"],
    "discipline": ["تسويف", "مؤجل", "procrastinat", "كسل", "مماطلة"],
    "motivation": ["محبط", "يائس", "تعب", "صعب", "hard", "tired", "إحباط"],
    "study": ["دراسة", "درس", "مذاكرة", "study", "learn", "تعلم"],
    "habit": ["عادة", "habit", "routine", "يومي", "روتين"],
    "progress": ["تقدم", "إنجاز", "progress", "achieve", "انجاز"],
    "plan": ["خطة", "plan", "schedule", "يوم", "جدول"],
    "goal": ["هدف", "goal", "target", "objective", " ambition"],
    "english": ["إنجليزي", "english", "language", "لغة انجليزية"],
    "programming": ["برمجة", "programming", "code", "coding", "برمج"],
    "cybersecurity": ["أمن", "cyber", "security", "hacking", "سيبراني"],
    "productivity": ["إنتاجية", "productivity", "focus", "تركيز", "انتاجية"],
    "accountability": ["محاسبة", "review", "summary", "today", "مراجعة"],
    "coach": ["coach", "مدرب", "نصيحة", "advice", "ارشاد"],
    "dashboard": ["dashboard", "لوحة", "stats", "statistics", "احصائيات"],
    "reminder": ["تذكير", "reminder", "تذكرني", "منبه"],
}

_COMMON_ERROR_FIXES = {
    "i am": "I am",
    "i was": "I was",
    "i have": "I have",
    "i will": "I will",
    "dont": "don't",
    "wont": "won't",
    "cant": "can't",
    "im ": "I'm ",
    "its ": "it's ",
    "your ": "you're ",
}

_COACH_ADVICE_LIST = [
    "ركز على مهمة واحدة في كل مرة. الجودة تفوق الكمية دائماً!",
    "ابدأ يومك بأصعب مهمة. بقية اليوم سيكون أسهل!",
    "خذ فترات راحة قصيرة. الدماغ يحتاج إلى استعادة طاقته.",
    "اكتب أهدافك كل صباح. الوضوح يولد النتائج!",
    "لا تقارن نفسك بالآخرين. رحلتك فريدة من نوعها!",
    "كل يوم تتعلم فيه شيئاً جديداً هو يوم ناجح!",
    "الانضباط ليس قسوة، بل هو حب لذاتك المستقبلية.",
    "العادات الصغيرة تُحدث فرقاً كبيراً على المدى الطويل.",
    "عندما تشعر بالتسويف، ابدأ بـ5 دقائق فقط. الزخم سيأتي!",
    "احتفل بإنجازاتك الصغيرة. كل خطوة تُحسب!",
]

_DEFAULT_CHAT_RESPONSES = [
    "أفهمك! دعني أساعدك في ذلك. ما الخطوة الأولى التي تريد البدء بها؟",
    "رائع! أنا هنا لدعمك. كيف يمكنني مساعدتك في تحقيق هذا؟",
    "ممتاز! لنخطط لذلك معاً. ما هو جدولك المتاح؟",
    "أحسنت على التفكير في هذا! دعنا نحوله إلى خطة عملية.",
]

class LocalProvider:
    """Offline fallback generator. Sync by design (no I/O)."""

    name = "local"

    _HANDLERS = {
        "chat": "_chat",
        "planning": "_planning",
        "explain_concept": "_explain_concept",
        "generate_test": "_generate_test",
        "correct_english": "_correct_english",
        "review_code": "_review_code",
        "performance_analysis": "_performance_analysis",
        "coach_advice": "_coach_advice",
        "english_lesson": "_english_lesson",
        "cyber_lesson": "_cyber_lesson",
        "grounded_answer": "_grounded_answer",
    }

    @property
    def is_available(self) -> bool:
        return True

    def generate(self, request: AIRequest) -> str:
        method_name = self._HANDLERS.get(request.capability)
        if method_name is None:
            raise AIProviderError(f"Local fallback has no generator for {request.capability}")
        return getattr(self, method_name)(request.data)

    # -- capability-specific canned generators -------------------------------
    def _chat(self, data: Dict[str, Any]) -> str:
        message = (data.get("message") or "").lower()
        context = (data.get("context") or "").lower()

        for response_key, keywords in _GREETING_KEYWORDS.items():
            if any(word in message for word in keywords):
                return LOCAL_AI_RESPONSES.get(response_key, LOCAL_AI_RESPONSES["greeting"])

        if "discipline" in context:
            return "⚡ وضع الانضباط مفعل! لا تسمح للتسويف بأن يسيطر عليك. ابدأ الآن!"
        if "study" in context:
            return "📚 جاهز للدراسة! ما الموضوع الذي تريد البدء به؟"
        if "habit" in context:
            return "🔄 بناء العادات يحتاج استمرارية! هل أكملت عادتك اليوم؟"

        return random.choice(_DEFAULT_CHAT_RESPONSES)

    def _planning(self, data: Dict[str, Any]) -> str:
        goals = data.get("goals") or []
        available_time = data.get("available_time") or "غير محدد"
        return (
            "📅 **خطتك اليومية:**\n\n"
            f"🎯 **الأهداف:** {', '.join(goals[:2]) if goals else available_time}\n\n"
            "📋 **المهام:**\n"
            "1️⃣ مهمة رئيسية (45 دقيقة)\n"
            "2️⃣ مراجعة سريعة (15 دقيقة)\n"
            "3️⃣ تطبيق عملي (30 دقيقة)\n\n"
            "⏰ **وقت المراجعة:** 15 دقيقة قبل النهاية\n"
            "☕ **وقت الراحة:** 10 دقائق بين كل مهمة\n\n"
            "💪 **نصيحة:** ابدأ بأصعب مهمة أولاً!"
        )

    def _explain_concept(self, data: Dict[str, Any]) -> str:
        concept = data.get("concept") or "المفهوم"
        subject = data.get("subject") or ""
        return (
            f"📚 **شرح: {concept}**\n\n"
            f"{subject} مفهوم مهم! إليك التبسيط:\n\n"
            f"🔹 **التعريف:** {concept} هو أحد المفاهيم الأساسية في {subject}.\n\n"
            "🔹 **لماذا مهم؟** لأنه يُشكل حجر الأساس للعديد من التطبيقات العملية.\n\n"
            "🔹 **مثال:** تخيل أنك... (سأكمل الشرح عند توفر المزيد من التفاصيل)\n\n"
            "💡 **نصيحة:** تطبق المفهوم عملياً يساعدك على فهمه بعمق!\n\n"
            "هل تريد أمثلة أكثر أو شرحاً أعمق؟ 🤔"
        )

    def _generate_test(self, data: Dict[str, Any]) -> str:
        topic = data.get("topic") or "الموضوع"
        difficulty = data.get("difficulty") or "medium"
        questions = {
            "easy": [
                "ما هو التعريف الأساسي لـ {topic}?",
                "اذكر مثالاً واحداً على {topic}.",
                "لماذا يعتبر {topic} مهمًا؟",
            ],
            "medium": [
                "اشرح {topic} بأسلوبك الخاص.",
                "ما الفرق بين {topic} والمفاهيم المشابهة؟",
                "كيف يمكن تطبيق {topic} في الحياة العملية؟",
                "ما هي خطوات تعلم {topic} بشكل صحيح؟",
            ],
            "hard": [
                "حل المشكلة التالية باستخدام {topic}...",
                "قارن بين {topic} وبدائله مع ذكر المميزات والعيوب.",
                "كيف يمكن تحسين أداء {topic} في السيناريو التالي...",
            ],
        }
        selected = questions.get(difficulty, questions["medium"])
        test_text = f"📝 **اختبار: {topic}**\n\n"
        for i, question in enumerate(selected, 1):
            test_text += f"{i}. {question.format(topic=topic)}\n\n"
        test_text += "✅ أرسل إجاباتك وسأصححها لك!"
        return test_text

    def _correct_english(self, data: Dict[str, Any]) -> str:
        text = data.get("text") or ""
        corrected = text
        corrections = []
        for error, replacement in _COMMON_ERROR_FIXES.items():
            if error in text.lower():
                corrections.append(f"'{error}' → '{replacement}'")
                corrected = corrected.replace(error, replacement, 1)
        if corrections:
            return (
                "✏️ **تصحيح:**\n\n"
                f"أخطاء موجودة:\n{chr(10).join(corrections)}\n\n"
                f"النص المصحح:\n{corrected}\n\n"
                "💡 **نصيحة:** ركز على كتابة I بحرف كبير دائماً!"
            )
        return "✅ نصك صحيح! أحسنت! هل تريد تحسينه أكثر؟"

    def _review_code(self, data: Dict[str, Any]) -> str:
        code = data.get("code") or ""
        language = data.get("language") or "python"
        issues = []
        suggestions = []

        if len(code) < 50:
            issues.append("الكود قصير جداً. هل هذا الجزء الكامل؟")
        if "#" not in code and language in ["python", "javascript", "java", "c++"]:
            suggestions.append("أضف تعليقات لتوضيح الكود")
        if any(err in code.lower() for err in ["error", "exception", "bug", "fixme"]):
            issues.append("يبدو أن هناك أخطاء في الكود. هل تريد مساعدة في حلها؟")
        if "print(" in code and language == "python":
            suggestions.append("استخدم logging بدلاً من print في المشاريع الكبيرة")
        if "password" in code.lower() or "secret" in code.lower():
            issues.append("⚠️ لا تخزن كلمات المرور مباشرة في الكود!")

        review = f"💻 **مراجعة الكود ({language}):**\n\n"
        if issues:
            review += "⚠️ **ملاحظات:**\n"
            for issue in issues:
                review += f"• {issue}\n"
            review += "\n"
        if suggestions:
            review += "💡 **اقتراحات التحسين:**\n"
            for suggestion in suggestions:
                review += f"• {suggestion}\n"
            review += "\n"
        review += "✅ الكود يبدو جيداً بشكل عام! هل تريد تحسينات أكثر تفصيلاً؟"
        return review

    def _performance_analysis(self, data: Dict[str, Any]) -> str:
        stats = data.get("stats") or {}
        study_hours = stats.get("weekly_study_hours", 0)
        tasks_completed = stats.get("weekly_tasks", 0)
        if study_hours > 20 and tasks_completed > 15:
            return "🌟 أداء ممتاز! أنت في المسار الصحيح. استمر على هذا الوتيرة!"
        if study_hours > 10 and tasks_completed > 7:
            return "👍 أداء جيد! يمكنك التحسين بزيادة التركيز. حاول إضافة ساعة إضافية هذا الأسبوع."
        return "⚡ أداء يحتاج تحسين! التزم بالخطة اليومية وحاسب نفسك كل ليلة. أنت قادر على الأفضل!"

    def _coach_advice(self, data: Dict[str, Any]) -> str:
        stats = data.get("stats") or {}
        if stats.get("weekly_study_hours", 0) < 5:
            return "⏰ أنت تدرس أقل من 5 ساعات أسبوعياً. حدد وقتًا ثابتًا للدراسة يوميًا ولا تتنازل عنه!"
        return f"🧠 **نصيحة مدربك:**\n\n{random.choice(_COACH_ADVICE_LIST)}"

    def _english_lesson(self, data: Dict[str, Any]) -> str:
        return (
            "🇬🇧 **درس الإنجليزية اليومي**\n\n"
            "📚 **مفردات جديدة:**\n"
            "1. Achieve - يحقق\n"
            "2. Consistent - متسق/منتظم\n"
            "3. Discipline - انضباط\n"
            "4. Progress - تقدم\n"
            "5. Habit - عادة\n\n"
            "📝 **قاعدة اليوم:**\n"
            "استخدم \"I have\" وليس \"I has\"\n"
            "✅ I have a goal\n"
            "❌ I has a goal\n\n"
            "💬 **محادثة:**\n"
            "A: What are you studying today?\n"
            "B: I'm learning programming. It's challenging but fun!\n\n"
            "✏️ **تمرين:**\n"
            "أكمل الجمل: \"I ______ (want/wants) to improve my English.\"\n\n"
            "💡 **خطأ شائع:**\n"
            "لا تنسَ حرف S في الأفعال مع He/She/It\n"
            "✅ He studies hard\n"
            "❌ He study hard"
        )

    def _cyber_lesson(self, data: Dict[str, Any]) -> str:
        topic = data.get("topic") or "الموضوع"
        return (
            f"🔒 **درس الأمن السيبراني: {topic}**\n\n"
            "📖 **ما هو؟**\n"
            f"{topic} هو أحد المجالات المهمة في الأمن السيبراني.\n\n"
            "🎯 **لماذا يهم؟**\n"
            "لأن الهجمات الإلكترونية في تزايد مستمر وتحتاج إلى حماية.\n\n"
            "🧪 **كيف تتعلمه؟**\n"
            "1. اقرأ عن الأساسيات\n"
            "2. جرب أدوات مثل TryHackMe أو HackTheBox\n"
            "3. تابع قنوات الأمن السيبراني\n\n"
            "🏆 **شهادات مقترحة:**\n"
            "• CompTIA Security+\n"
            "• CEH (Certified Ethical Hacker)\n"
            "• OSCP (Offensive Security)\n\n"
            "هل تريد تفاصيل أكثر عن أي جانب؟ 🔍"
        )

    def _grounded_answer(self, data: Dict[str, Any]) -> str:
        """Deterministic grounded answer: quote the best evidence + citations.

        The retrieved text is DATA, never instructions - the generator quotes
        it verbatim and never acts on anything written inside it. Page numbers
        are only shown when the evidence actually carries one.
        """
        question = (data.get("question") or "").strip()
        evidence = (data.get("evidence") or {}).get("blocks") or []
        if not evidence:
            return (
                "🔍 لم أجد إجابة على هذا السؤال في مستنداتك.\n\n"
                "جرّب سؤالاً آخر أو أضف كتاباً يحتوي على الموضوع."
            )

        block = evidence[0]
        snippet = block.get("block", "")
        if "\n" in snippet:
            snippet = snippet.split("\n", 1)[1].strip()

        # Deliberately static, token-limited quote of the retrieved data.
        excerpt = snippet[:300].strip()
        if len(snippet) > 300:
            excerpt += "…"

        lines = [
            "📚 **إجابة بناءً على كتابك:**",
            f"عن: {question}\n",
            excerpt,
            "",
            "📌 **المصدر:**",
        ]
        source = block.get("source")
        if source:
            lines.append(f"📖 {source}")
        page = block.get("page")
        if page:
            lines.append(f"ص. {page}")
        if len(block.get("block", "")) > 300:
            lines.append("\n💡 اقرأ المزيد في الكتاب نفسه للحصول على التفاصيل الكاملة.")
        return "\n".join(lines)