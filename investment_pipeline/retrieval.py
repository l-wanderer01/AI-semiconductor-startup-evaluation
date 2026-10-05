from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import pickle
from pathlib import Path
import re
from typing import Iterable, List

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
from sklearn.feature_extraction.text import TfidfVectorizer

from evaluation.integration import observed_search
from evaluation.recording import CURRENT, traced
from evaluation.runtime import local_embedding

from .config import settings
from .models import ResearchEvidence


def format_docs(docs: List[Document]) -> str:
    return "\n".join(
        [
            (
                f"<document><content>{doc.page_content}</content>"
                f"<source>{doc.metadata.get('source', '')}</source>"
                f"<page>{doc.metadata.get('page', '')}</page></document>"
            )
            for doc in docs
        ]
    )


_qdrant_client: QdrantClient | None = None
_dense_embedder: SentenceTransformer | None = None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


def _resolve_local_sentence_transformer_path(model_name: str) -> str:
    home = Path.home()
    hub_dir = home / ".cache" / "huggingface" / "hub"
    model_dir = hub_dir / f"models--{model_name.replace('/', '--')}"
    ref_path = model_dir / "refs" / "main"
    snapshot_root = model_dir / "snapshots"
    if ref_path.exists():
        revision = ref_path.read_text(encoding="utf-8").strip()
        snapshot_path = snapshot_root / revision
        if snapshot_path.exists():
            return str(snapshot_path)
    if snapshot_root.exists():
        snapshots = sorted(path for path in snapshot_root.iterdir() if path.is_dir())
        if snapshots:
            return str(snapshots[-1])
    return model_name


def get_qdrant_client() -> QdrantClient:
    global _qdrant_client
    if _qdrant_client is None:
        settings.qdrant_path.mkdir(parents=True, exist_ok=True)
        _qdrant_client = QdrantClient(path=str(settings.qdrant_path))
    return _qdrant_client


def get_dense_embedder() -> SentenceTransformer:
    global _dense_embedder
    if _dense_embedder is None:
        model_path = _resolve_local_sentence_transformer_path(settings.dense_embedding_model)
        _dense_embedder = SentenceTransformer(
            model_path,
            local_files_only=True,
            trust_remote_code=False,
        )
    return _dense_embedder


@traced('dense_embedding')
@local_embedding
def _dense_vectors(texts: List[str]) -> List[List[float]]:
    embeddings = get_dense_embedder().encode(texts, normalize_embeddings=True)
    return [list(vector) for vector in embeddings]


def _fit_sparse_vectorizer(texts: List[str]) -> TfidfVectorizer:
    vectorizer = TfidfVectorizer(
        lowercase=True,
        stop_words=None,
        ngram_range=(1, 2),
        max_features=50000,
    )
    vectorizer.fit(texts)
    return vectorizer


def _sparse_vectorizer_path(collection_name: str) -> Path:
    return settings.qdrant_path / f"{collection_name}.vectorizer.pkl"


def _save_sparse_vectorizer(collection_name: str, vectorizer: TfidfVectorizer) -> None:
    with _sparse_vectorizer_path(collection_name).open("wb") as file:
        pickle.dump(vectorizer, file)


def _load_sparse_vectorizer(collection_name: str) -> TfidfVectorizer:
    with _sparse_vectorizer_path(collection_name).open("rb") as file:
        return pickle.load(file)


def _sparse_vectors(texts: List[str], vectorizer: TfidfVectorizer) -> List[models.SparseVector]:
    matrix = vectorizer.transform(texts)
    vectors: List[models.SparseVector] = []
    for row in matrix:
        coo = row.tocoo()
        vectors.append(
            models.SparseVector(
                indices=[int(index) for index in coo.col.tolist()],
                values=[float(value) for value in coo.data.tolist()],
            )
        )
    return vectors


def _fingerprint(docs: Iterable[Document]) -> str:
    joined = "\n".join(
        f"{doc.page_content}::{json.dumps(doc.metadata, sort_keys=True, ensure_ascii=False)}" for doc in docs
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


@dataclass
class QdrantHybridKnowledgeBase:
    collection_name: str
    fingerprint: str

    @classmethod
    def build(cls, *, collection_name: str, docs: List[Document]) -> "QdrantHybridKnowledgeBase":
        recorder = CURRENT.get()
        if recorder is not None:
            for doc in docs:
                recorder.add_evidence(doc.page_content, source=str(doc.metadata.get('source', 'unknown')),
                                      title=str(doc.metadata.get('title', collection_name)))
        client = get_qdrant_client()
        fingerprint = _fingerprint(docs)
        meta_path = settings.qdrant_path / f"{collection_name}.meta.json"
        current_meta = None
        if meta_path.exists():
            current_meta = json.loads(meta_path.read_text(encoding="utf-8"))

        if current_meta is None or current_meta.get("fingerprint") != fingerprint:
            texts = [doc.page_content for doc in docs]
            dense_vectors = _dense_vectors(texts)
            vectorizer = _fit_sparse_vectorizer(texts)
            sparse_vectors = _sparse_vectors(texts, vectorizer)

            if client.collection_exists(collection_name):
                client.delete_collection(collection_name)

            client.create_collection(
                collection_name=collection_name,
                vectors_config={
                    "dense": models.VectorParams(
                        size=len(dense_vectors[0]),
                        distance=models.Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    "sparse": models.SparseVectorParams()
                },
            )

            client.upsert(
                collection_name=collection_name,
                points=[
                    models.PointStruct(
                        id=index,
                        vector={
                            "dense": dense_vectors[index],
                            "sparse": sparse_vectors[index],
                        },
                        payload={
                            "page_content": docs[index].page_content,
                            "metadata": docs[index].metadata,
                        },
                    )
                    for index in range(len(docs))
                ],
            )
            _save_sparse_vectorizer(collection_name, vectorizer)
            meta_path.write_text(
                json.dumps({"fingerprint": fingerprint}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        return cls(collection_name=collection_name, fingerprint=fingerprint)

    @observed_search()
    def search(self, query: str, limit: int | None = None) -> List[Document]:
        client = get_qdrant_client()
        limit = limit or settings.hybrid_search_limit
        dense_query = _dense_vectors([query])[0]
        vectorizer = _load_sparse_vectorizer(self.collection_name)
        sparse_query = _sparse_vectors([query], vectorizer)[0]

        result = client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                models.Prefetch(
                    query=dense_query,
                    using="dense",
                    limit=max(limit * 3, 6),
                ),
                models.Prefetch(
                    query=sparse_query,
                    using="sparse",
                    limit=max(limit * 3, 6),
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            with_payload=True,
            limit=limit,
        )

        docs: List[Document] = []
        for point in result.points:
            payload = point.payload or {}
            docs.append(
                Document(
                    page_content=payload.get("page_content", ""),
                    metadata=payload.get("metadata", {}),
                )
            )
        return docs


@dataclass
class DesignDocumentKnowledgeBase:
    source_path: Path
    hybrid_kb: QdrantHybridKnowledgeBase

    @classmethod
    def from_markdown(cls, source_path: Path) -> "DesignDocumentKnowledgeBase":
        raw_text = source_path.read_text(encoding="utf-8")
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=1200,
            chunk_overlap=150,
            separators=["\n## ", "\n### ", "\n", " "],
        )
        split_texts = splitter.split_text(raw_text)
        docs = [
            Document(
                page_content=chunk,
                metadata={"source": str(source_path), "page": index + 1},
            )
            for index, chunk in enumerate(split_texts)
        ]
        collection_name = f"design_doc_{_slug(source_path.stem)}"
        hybrid_kb = QdrantHybridKnowledgeBase.build(collection_name=collection_name, docs=docs)
        return cls(source_path=source_path, hybrid_kb=hybrid_kb)

    def search(self, query: str, limit: int = 4) -> List[Document]:
        return self.hybrid_kb.search(query, limit=limit)


def build_evidence_knowledge_base(
    *,
    key: str,
    evidence: List[ResearchEvidence],
) -> QdrantHybridKnowledgeBase | None:
    if not evidence:
        return None

    docs = [
        Document(
            page_content=item.content,
            metadata={
                "source": item.url,
                "page": index + 1,
                "title": item.title,
                "category": item.category,
                "published_date": item.published_date or "",
            },
        )
        for index, item in enumerate(evidence)
        if item.content
    ]
    if not docs:
        return None

    collection_name = f"evidence_{_slug(key)}"
    return QdrantHybridKnowledgeBase.build(collection_name=collection_name, docs=docs)
