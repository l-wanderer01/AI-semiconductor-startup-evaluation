from __future__ import annotations

import json
from pathlib import Path
from typing import List

import requests

from evaluation.integration import observed_search

from .config import settings
from .models import ResearchEvidence


class TavilySearchClient:
    search_url = "https://api.tavily.com/search"
    blocked_domains = {
        "linkedin.com",
        "www.linkedin.com",
        "medium.com",
        "www.medium.com",
        "wikipedia.org",
        "en.wikipedia.org",
        "tracxn.com",
        "www.tracxn.com",
        "superbcrew.com",
        "www.superbcrew.com",
    }

    def __init__(self, api_key: str | None) -> None:
        self.api_key = api_key

    @property
    def available(self) -> bool:
        return bool(self.api_key and settings.enable_live_research)

    @observed_search(web=True)
    def search(self, query: str, *, category: str, days: int = 3650) -> List[ResearchEvidence]:
        if not self.available:
            return []

        cache_dir = settings.research_cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_key = f"{category}__{query}".replace("/", "_").replace(" ", "_")[:180]
        cache_file = cache_dir / f"{cache_key}.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return self._filter([ResearchEvidence.model_validate(item) for item in data])

        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": "advanced",
            "topic": "general",
            "max_results": settings.tavily_max_results_per_query,
            "include_answer": False,
            "include_raw_content": False,
            "days": days,
        }
        response = requests.post(self.search_url, json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
        evidence: List[ResearchEvidence] = []
        for item in data.get("results", []):
            evidence.append(
                ResearchEvidence(
                    title=item.get("title") or query,
                    url=item.get("url") or "",
                    source=item.get("url", "").split("/")[2] if item.get("url") else "unknown",
                    published_date=item.get("published_date"),
                    content=(item.get("content") or "").strip(),
                    score=item.get("score"),
                    category=category,
                )
            )
        cache_file.write_text(
            json.dumps([item.model_dump(mode="json") for item in evidence], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self._filter(evidence)

    def _filter(self, evidence: List[ResearchEvidence]) -> List[ResearchEvidence]:
        filtered: List[ResearchEvidence] = []
        seen_urls = set()
        for item in evidence:
            if not item.url:
                continue
            domain = item.source.lower()
            if any(domain.endswith(blocked) or domain == blocked for blocked in self.blocked_domains):
                continue
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            filtered.append(item)
        return filtered


tavily_client = TavilySearchClient(settings.tavily_api_key)
