"""Shared keyword/vector history retrieval for the web UI and resident MCP."""

from __future__ import annotations

import hashlib
import math
import struct
import time
from typing import Any, Protocol

from .store import Database

try:
    import sqlite_vec  # type: ignore
except ImportError:  # Optional at test/dev time; Python cosine remains correct.
    sqlite_vec = None


class Embedder(Protocol):
    @property
    def model_name(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _pack(vector: list[float]) -> bytes:
    return struct.pack("<" + "f" * len(vector), *vector)


def _unpack(blob: bytes, dims: int) -> tuple[float, ...]:
    return struct.unpack("<" + "f" * dims, bytes(blob))


def _cosine(left: list[float] | tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return -1.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


class ProviderEmbedder:
    def __init__(self, provider: Any, model_name: str):
        self.provider = provider
        self._model_name = str(model_name or "")

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.provider.embed(texts)


class HistorySearchService:
    """One retrieval path shared by browser search, MCP search and passive recall."""

    def __init__(self, db: Database, embedder: Embedder | None = None):
        self.db = db
        self.embedder = embedder
        self.vector_backend = "sqlite-vec" if sqlite_vec is not None else "python-cosine"

    def set_embedder(self, embedder: Embedder | None) -> None:
        """Switch the configured provider without rebuilding the web/MCP service."""
        self.embedder = embedder
        self.vector_backend = "sqlite-vec" if sqlite_vec is not None else "python-cosine"

    @property
    def semantic_available(self) -> bool:
        return bool(self.embedder and self.embedder.model_name)

    @staticmethod
    def _where(
        chat_id: str | None,
        kinds: tuple[str, ...],
        date_from: float | None,
        date_to: float | None,
        alias: str = "m",
    ) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if chat_id:
            clauses.append(f"{alias}.chat_id=?")
            args.append(chat_id)
        if kinds:
            clauses.append(f"{alias}.kind IN (" + ",".join("?" for _ in kinds) + ")")
            args.extend(kinds)
        if date_from is not None:
            clauses.append(f"{alias}.at>=?")
            args.append(float(date_from))
        if date_to is not None:
            clauses.append(f"{alias}.at<=?")
            args.append(float(date_to))
        return clauses, args

    def keyword_search(
        self,
        query: str,
        chat_id: str | None,
        kinds: tuple[str, ...],
        date_from: float | None,
        date_to: float | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses, args = self._where(chat_id, kinds, date_from, date_to)
        ranked: list[dict[str, Any]] = []
        seen: set[int] = set()
        if len(query) >= 3:
            safe = query.replace('"', '""')
            try:
                fts_rows = self.db.query(
                    "SELECT m.seq,m.chat_id,m.kind,m.text,m.at,bm25(message_fts) AS rank "
                    "FROM message_fts f JOIN messages m ON m.seq=f.rowid "
                    "WHERE message_fts MATCH ?"
                    + (" AND " + " AND ".join(clauses) if clauses else "")
                    + " ORDER BY rank LIMIT ?",
                    ['"' + safe + '"', *args, max(limit * 3, 30)],
                )
            except Exception:
                fts_rows = []
            for row in fts_rows:
                seq = int(row["seq"])
                if seq not in seen:
                    seen.add(seq)
                    ranked.append(row)
        like = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
        like_rows = self.db.query(
            "SELECT m.seq,m.chat_id,m.kind,m.text,m.at,0.0 AS rank FROM messages m "
            "WHERE m.text LIKE ? ESCAPE '\\'"
            + (" AND " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY m.seq DESC LIMIT ?",
            [like, *args, max(limit * 3, 30)],
        )
        for row in like_rows:
            seq = int(row["seq"])
            if seq not in seen:
                seen.add(seq)
                ranked.append(row)
        return ranked[: max(1, limit)]

    @staticmethod
    def _turn_segments(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        turns: list[list[dict[str, Any]]] = []
        for row in rows:
            if row.get("kind") == "me" or not turns:
                turns.append([row])
            else:
                turns[-1].append(row)
        segments: list[dict[str, Any]] = []
        for size in (3, 6):
            for start in range(0, len(turns) - size + 1):
                window = [row for turn in turns[start : start + size] for row in turn]
                text = "\n".join(
                    ("用户：" if row.get("kind") == "me" else "驻客：")
                    + str(row.get("text") or "")
                    for row in window
                )[:24000]
                if text.strip():
                    segments.append({
                        "start_seq": int(window[0]["seq"]),
                        "end_seq": int(window[-1]["seq"]),
                        "text": text,
                    })
        return segments

    def refresh_embeddings(self, chat_id: str, max_messages: int = 2000) -> dict[str, Any]:
        if not self.semantic_available:
            return {"available": False, "indexed": 0, "pending": 0}
        assert self.embedder is not None
        model = self.embedder.model_name
        all_rows = self.db.query(
            "SELECT seq,chat_id,kind,text,at FROM messages "
            "WHERE chat_id=? AND kind IN ('me','gu') ORDER BY seq",
            (chat_id,),
        )
        existing = {
            int(row["message_seq"]): str(row["text_hash"])
            for row in self.db.query(
                "SELECT message_seq,text_hash FROM message_embeddings WHERE chat_id=? AND model=?",
                (chat_id, model),
            )
        }
        pending_all = [
            row for row in all_rows
            if existing.get(int(row["seq"])) != _hash(str(row.get("text") or ""))
        ]
        pending_rows = pending_all[: max(1, int(max_messages))]
        indexed = 0
        for offset in range(0, len(pending_rows), 64):
            batch = pending_rows[offset : offset + 64]
            vectors = self.embedder.embed([str(row.get("text") or "") for row in batch])
            now = time.time()
            with self.db.connect() as conn:
                conn.executemany(
                    "INSERT INTO message_embeddings"
                    "(message_seq,chat_id,kind,model,dims,text_hash,embedding,updated) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(message_seq) DO UPDATE SET "
                    "chat_id=excluded.chat_id,kind=excluded.kind,model=excluded.model,"
                    "dims=excluded.dims,text_hash=excluded.text_hash,embedding=excluded.embedding,"
                    "updated=excluded.updated",
                    [
                        (
                            int(row["seq"]), chat_id, str(row["kind"]), model, len(vector),
                            _hash(str(row.get("text") or "")), _pack(vector), now,
                        )
                        for row, vector in zip(batch, vectors)
                    ],
                )
            indexed += len(batch)

        segments = self._turn_segments(all_rows) if len(pending_all) <= len(pending_rows) else []
        existing_segments = {
            (int(row["start_seq"]), int(row["end_seq"])): str(row["text_hash"])
            for row in self.db.query(
                "SELECT start_seq,end_seq,text_hash FROM message_embedding_segments "
                "WHERE chat_id=? AND model=?",
                (chat_id, model),
            )
        }
        wanted = {(item["start_seq"], item["end_seq"]): item for item in segments}
        with self.db.connect() as conn:
            for key in set(existing_segments) - set(wanted):
                conn.execute(
                    "DELETE FROM message_embedding_segments WHERE chat_id=? AND model=? "
                    "AND start_seq=? AND end_seq=?",
                    (chat_id, model, key[0], key[1]),
                )
        pending_segments = [
            item for key, item in wanted.items()
            if existing_segments.get(key) != _hash(item["text"])
        ]
        for offset in range(0, len(pending_segments), 32):
            batch = pending_segments[offset : offset + 32]
            vectors = self.embedder.embed([item["text"] for item in batch])
            now = time.time()
            with self.db.connect() as conn:
                conn.executemany(
                    "INSERT INTO message_embedding_segments"
                    "(chat_id,start_seq,end_seq,text,model,dims,text_hash,embedding,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,start_seq,end_seq,model) "
                    "DO UPDATE SET text=excluded.text,dims=excluded.dims,text_hash=excluded.text_hash,"
                    "embedding=excluded.embedding,updated=excluded.updated",
                    [
                        (
                            chat_id, item["start_seq"], item["end_seq"], item["text"], model,
                            len(vector), _hash(item["text"]), _pack(vector), now,
                        )
                        for item, vector in zip(batch, vectors)
                    ],
                )
            indexed += len(batch)
        return {
            "available": True,
            "model": model,
            "indexed": indexed,
            "pending": max(0, len(pending_all) - len(pending_rows)),
        }

    def _semantic_rows(
        self,
        query_vector: list[float],
        chat_id: str,
        kinds: tuple[str, ...],
        date_from: float | None,
        date_to: float | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        assert self.embedder is not None
        model = self.embedder.model_name
        clauses, args = self._where(chat_id, kinds, date_from, date_to)
        if sqlite_vec is not None:
            try:
                query_blob = _pack(query_vector)
                with self.db.connect() as conn:
                    sqlite_vec.load(conn)
                    rows = [dict(row) for row in conn.execute(
                        "SELECT m.seq,m.chat_id,m.kind,m.text,m.at,e.dims,e.embedding,"
                        "1.0-vec_distance_cosine(e.embedding,?) AS similarity "
                        "FROM message_embeddings e JOIN messages m ON m.seq=e.message_seq "
                        "WHERE e.model=? AND e.dims=?"
                        + (" AND " + " AND ".join(clauses) if clauses else "")
                        + " ORDER BY similarity DESC LIMIT ?",
                        [query_blob, model, len(query_vector), *args, max(limit * 3, 30)],
                    ).fetchall()]
                    if kinds == ("me", "gu") or set(kinds) == {"me", "gu"}:
                        segment_rows = [dict(row) for row in conn.execute(
                            "SELECT s.start_seq AS seq,s.end_seq AS segment_end_seq,"
                            "s.text,m.at,m.kind,m.chat_id,"
                            "1.0-vec_distance_cosine(s.embedding,?) AS similarity "
                            "FROM message_embedding_segments s "
                            "JOIN messages m ON m.seq=s.start_seq "
                            "WHERE s.chat_id=? AND s.model=? AND s.dims=?"
                            + (" AND m.at>=?" if date_from is not None else "")
                            + (" AND m.at<=?" if date_to is not None else "")
                            + " ORDER BY similarity DESC LIMIT ?",
                            [query_blob, chat_id, model, len(query_vector)]
                            + ([float(date_from)] if date_from is not None else [])
                            + ([float(date_to)] if date_to is not None else [])
                            + [max(limit * 3, 30)],
                        ).fetchall()]
                    else:
                        segment_rows = []
                self.vector_backend = "sqlite-vec"
                combined = [
                    *({**row, "match_type": "message"} for row in rows),
                    *({**row, "match_type": "segment"} for row in segment_rows),
                ]
                combined.sort(key=lambda row: float(row.get("similarity") or -1), reverse=True)
                return combined[: max(limit * 3, 30)]
            except Exception:
                # Correctness-first fallback keeps semantic search available if a
                # particular sqlite-vec build cannot load on this host.
                self.vector_backend = "python-cosine"
        rows = self.db.query(
            "SELECT m.seq,m.chat_id,m.kind,m.text,m.at,e.dims,e.embedding "
            "FROM message_embeddings e JOIN messages m ON m.seq=e.message_seq "
            "WHERE e.model=?" + (" AND " + " AND ".join(clauses) if clauses else ""),
            [model, *args],
        )
        scored: list[dict[str, Any]] = []
        for row in rows:
            similarity = _cosine(query_vector, _unpack(row["embedding"], int(row["dims"])))
            if similarity > -1:
                scored.append({**row, "similarity": similarity, "match_type": "message"})
        if kinds == ("me", "gu") or set(kinds) == {"me", "gu"}:
            segment_rows = self.db.query(
                "SELECT s.id,s.start_seq,s.end_seq,s.text,s.dims,s.embedding,m.at,m.kind,m.chat_id "
                "FROM message_embedding_segments s JOIN messages m ON m.seq=s.start_seq "
                "WHERE s.chat_id=? AND s.model=?"
                + (" AND m.at>=?" if date_from is not None else "")
                + (" AND m.at<=?" if date_to is not None else ""),
                [chat_id, model]
                + ([float(date_from)] if date_from is not None else [])
                + ([float(date_to)] if date_to is not None else []),
            )
            for row in segment_rows:
                similarity = _cosine(query_vector, _unpack(row["embedding"], int(row["dims"])))
                if similarity > -1:
                    scored.append({
                        "seq": int(row["start_seq"]),
                        "chat_id": row["chat_id"],
                        "kind": row["kind"],
                        "text": row["text"],
                        "at": row["at"],
                        "similarity": similarity,
                        "match_type": "segment",
                        "segment_end_seq": int(row["end_seq"]),
                    })
        scored.sort(key=lambda row: float(row["similarity"]), reverse=True)
        return scored[: max(limit * 3, 30)]

    def search(
        self,
        query: str,
        mode: str = "hybrid",
        chat_id: str | None = None,
        kinds: tuple[str, ...] = ("me", "gu"),
        date_from: float | None = None,
        date_to: float | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query:
            return {"results": [], "semantic": {"available": self.semantic_available}}
        if mode not in ("keyword", "semantic", "hybrid"):
            raise ValueError("mode must be keyword, semantic, or hybrid")
        limit = max(1, min(int(limit), 50))
        keyword = self.keyword_search(
            query, chat_id, kinds, date_from, date_to, max(limit * 3, 30)
        ) if mode in ("keyword", "hybrid") else []
        semantic: list[dict[str, Any]] = []
        semantic_status: dict[str, Any] = {"available": self.semantic_available}
        if mode in ("semantic", "hybrid") and self.semantic_available and chat_id:
            try:
                semantic_status.update(self.refresh_embeddings(chat_id))
                assert self.embedder is not None
                query_vector = self.embedder.embed([query])[0]
                semantic = self._semantic_rows(
                    query_vector, chat_id, kinds, date_from, date_to, limit
                )
                semantic_status["vector_backend"] = self.vector_backend
            except Exception as exc:
                semantic_status = {
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        ranks: dict[tuple[int, str], dict[str, Any]] = {}
        for source, rows in (("keyword", keyword), ("semantic", semantic)):
            for rank, row in enumerate(rows, 1):
                key = (int(row["seq"]), str(row.get("match_type") or "message"))
                item = ranks.setdefault(key, {**row, "score": 0.0, "sources": []})
                item["score"] += 1.0 / (60.0 + rank)
                item["sources"].append(source)
                if source == "semantic":
                    item["similarity"] = float(row.get("similarity") or 0)
        results = sorted(
            ranks.values(),
            key=lambda row: (float(row["score"]), float(row.get("similarity") or -1), int(row["seq"])),
            reverse=True,
        )[:limit]
        return {"results": results, "semantic": semantic_status, "mode": mode}
