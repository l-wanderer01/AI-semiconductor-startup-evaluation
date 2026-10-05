from __future__ import annotations

from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from evaluation.recording import ObservedRunnable, traced
from evaluation.models import ModelSettings

from .config import settings

T = TypeVar("T", bound=BaseModel)

try:
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover - optional until dependencies are installed
    ChatOpenAI = None


class LLMClient:
    def __init__(self) -> None:
        self._model = None
        self._ensure_model()

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        if ChatOpenAI is None or not settings.openai_api_key or not settings.enable_llm_enrichment:
            return
        try:
            self._model = ChatOpenAI(
                model=settings.openai_model,
                temperature=settings.temperature,
                api_key=settings.openai_api_key,
                timeout=20,
                max_retries=0,
            )
            self._model = ObservedRunnable(self._model, kind='llm', name='pipeline_llm',
                                           model=ModelSettings(provider='openai', model=settings.openai_model,
                                                               temperature=settings.temperature, max_retries=0))
        except Exception:
            self._model = None

    @property
    def available(self) -> bool:
        self._ensure_model()
        return self._model is not None

    @traced('invoke_structured')
    def invoke_structured(self, prompt: str, schema: Type[T]) -> Optional[T]:
        self._ensure_model()
        if not self._model:
            return None
        try:
            structured_model = self._model.with_structured_output(schema)
            return structured_model.invoke(prompt)
        except Exception:
            return None

    @traced('invoke_text')
    def invoke_text(self, prompt: str) -> Optional[str]:
        self._ensure_model()
        if not self._model:
            return None
        try:
            response = self._model.invoke(prompt)
            return getattr(response, "content", None)
        except Exception:
            return None


llm_client = LLMClient()
