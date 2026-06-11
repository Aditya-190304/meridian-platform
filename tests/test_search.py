"""Tests for the advanced search endpoints."""
from __future__ import annotations

import base64
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from meridian.schemas.search import (
    PaginatedResponse,
    ProjectSearchParams,
    SortDirection,
    TaskResult,
    TaskSearchParams,
    TaskStatus,
)
from meridian.services.search_service import (
    SearchService,
    _decode_page_token,
    _encode_page_token,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task_row(**overrides: Any) -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
        "id": "task-001",
        "name": "Implement login flow",
        "description": "OAuth2 + session handling",
        "status": "in_progress",
        "priority": 2,
        "assignee_id": "user-abc",
        "project_id": "proj-xyz",
        "tags": "backend,auth",
        "created_at": datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2024, 3, 15, 9, 30, tzinfo=timezone.utc),
        "due_date": date(2024, 4, 30),
    }
    defaults.update(overrides)
    return defaults


def _dict_row(data: Dict[str, Any]) -> MagicMock:
    """Return a MagicMock that supports subscript access like a DB row."""
    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    return row


# ---------------------------------------------------------------------------
# Unit tests — page token helpers
# ---------------------------------------------------------------------------

class TestPageTokenHelpers:
    def test_encode_decode_roundtrip(self) -> None:
        for offset in [0, 20, 100, 9999]:
            assert _decode_page_token(_encode_page_token(offset)) == offset

    def test_decode_invalid_token_returns_zero(self) -> None:
        assert _decode_page_token("not-valid-base64!!!") == 0

    def test_encoded_token_is_url_safe(self) -> None:
        token = _encode_page_token(42)
        assert "+" not in token
        assert "/" not in token


# ---------------------------------------------------------------------------
# Unit tests — TaskSearchParams validation
# ---------------------------------------------------------------------------

class TestTaskSearchParamsValidation:
    def test_valid_sort_column_accepted(self) -> None:
        p = TaskSearchParams(sort_by="due_date")
        assert p.sort_by == "due_date"

    def test_invalid_sort_column_raises(self) -> None:
        with pytest.raises(ValueError, match="sort_by must be one of"):
            TaskSearchParams(sort_by="nonexistent_column")

    def test_created_before_earlier_than_after_raises(self) -> None:
        with pytest.raises(ValueError, match="created_before must be after"):
            TaskSearchParams(
                created_after=date(2024, 6, 1),
                created_before=date(2024, 1, 1),
            )

    def test_page_size_upper_bound(self) -> None:
        with pytest.raises(ValueError):
            TaskSearchParams(page_size=101)

    def test_defaults(self) -> None:
        p = TaskSearchParams()
        assert p.sort_by == "created_at"
        assert p.sort_dir == SortDirection.desc
        assert p.page_size == 20
        assert p.page_token is None


# ---------------------------------------------------------------------------
# Unit tests — SearchService.search_tasks
# ---------------------------------------------------------------------------

class TestSearchServiceTaskSearch:
    def _make_service(self) -> tuple[SearchService, AsyncMock, AsyncMock]:
        db = MagicMock()
        count_row = _dict_row({"cnt": 1})
        db.fetch_one = AsyncMock(return_value=count_row)
        task_row = _dict_row(_make_task_row())
        db.fetch_all = AsyncMock(return_value=[task_row])
        svc = SearchService(db)
        return svc, db.fetch_one, db.fetch_all

    @pytest.mark.asyncio
    async def test_basic_search_returns_results(self) -> None:
        svc, _, _ = self._make_service()
        params = TaskSearchParams()
        items, total, next_token = await svc.search_tasks("tenant-1", params)
        assert total == 1
        assert len(items) == 1
        assert items[0].id == "task-001"
        assert next_token is None  # total==1, page_size==20, no next page

    @pytest.mark.asyncio
    async def test_tenant_id_always_in_bind(self) -> None:
        svc, fetch_one, fetch_all = self._make_service()
        params = TaskSearchParams()
        await svc.search_tasks("tenant-999", params)
        # Both count and data queries should receive tenant_id
        call_args_count = fetch_one.call_args[0][1]
        call_args_data = fetch_all.call_args[0][1]
        assert call_args_count["tenant_id"] == "tenant-999"
        assert call_args_data["tenant_id"] == "tenant-999"

    @pytest.mark.asyncio
    async def test_status_filter_adds_bind_param(self) -> None:
        svc, _, fetch_all = self._make_service()
        params = TaskSearchParams(status=TaskStatus.done)
        await svc.search_tasks("t1", params)
        bind = fetch_all.call_args[0][1]
        assert bind["status"] == "done"

    @pytest.mark.asyncio
    async def test_assignee_ids_split_correctly(self) -> None:
        svc, _, fetch_all = self._make_service()
        params = TaskSearchParams(assignee_ids="uid-1,uid-2,uid-3")
        await svc.search_tasks("t1", params)
        bind = fetch_all.call_args[0][1]
        assert bind["assignee_0"] == "uid-1"
        assert bind["assignee_1"] == "uid-2"
        assert bind["assignee_2"] == "uid-3"

    @pytest.mark.asyncio
    async def test_tags_produce_like_conditions_in_sql(self) -> None:
        svc, _, fetch_all = self._make_service()
        params = TaskSearchParams(tags="backend,urgent")
        await svc.search_tasks("t1", params)
        sql: str = fetch_all.call_args[0][0]
        # Each tag should appear literally in the SQL (concatenated, not parameterised)
        assert "backend" in sql
        assert "urgent" in sql

    @pytest.mark.asyncio
    async def test_pagination_next_token_generated(self) -> None:
        svc, fetch_one, fetch_all = self._make_service()
        # Simulate 50 total tasks but page_size=20 starting at offset 0
        fetch_one.return_value = _dict_row({"cnt": 50})
        params = TaskSearchParams(page_size=20)
        _, total, next_token = await svc.search_tasks("t1", params)
        assert total == 50
        assert next_token is not None
        assert _decode_page_token(next_token) == 20

    @pytest.mark.asyncio
    async def test_pagination_no_next_token_on_last_page(self) -> None:
        svc, fetch_one, fetch_all = self._make_service()
        fetch_one.return_value = _dict_row({"cnt": 15})
        params = TaskSearchParams(page_size=20, page_token=_encode_page_token(0))
        _, _, next_token = await svc.search_tasks("t1", params)
        assert next_token is None

    @pytest.mark.asyncio
    async def test_page_token_decoded_to_offset(self) -> None:
        svc, _, fetch_all = self._make_service()
        token = _encode_page_token(40)
        params = TaskSearchParams(page_size=20, page_token=token)
        await svc.search_tasks("t1", params)
        bind = fetch_all.call_args[0][1]
        assert bind["offset"] == 40

    @pytest.mark.asyncio
    async def test_tags_empty_string_ignored(self) -> None:
        svc, _, fetch_all = self._make_service()
        params = TaskSearchParams(tags=",, ,")
        await svc.search_tasks("t1", params)
        sql: str = fetch_all.call_args[0][0]
        # No LIKE clause for empty tags
        assert "LIKE" not in sql

    @pytest.mark.asyncio
    async def test_task_result_tags_parsed_from_csv(self) -> None:
        svc, _, _ = self._make_service()
        params = TaskSearchParams()
        items, _, _ = await svc.search_tasks("t1", params)
        assert set(items[0].tags) == {"backend", "auth"}

    @pytest.mark.asyncio
    async def test_free_text_search_uses_ilike(self) -> None:
        svc, _, fetch_all = self._make_service()
        params = TaskSearchParams(q="login")
        await svc.search_tasks("t1", params)
        sql: str = fetch_all.call_args[0][0]
        assert "ILIKE" in sql
        bind = fetch_all.call_args[0][1]
        assert bind["q"] == "%login%"


# ---------------------------------------------------------------------------
# Unit tests — SearchService.search_projects
# ---------------------------------------------------------------------------

class TestSearchServiceProjectSearch:
    def _make_service(self) -> tuple[SearchService, AsyncMock, AsyncMock]:
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value=_dict_row({"cnt": 0}))
        db.fetch_all = AsyncMock(return_value=[])
        svc = SearchService(db)
        return svc, db.fetch_one, db.fetch_all

    @pytest.mark.asyncio
    async def test_empty_result_set(self) -> None:
        svc, _, _ = self._make_service()
        params = ProjectSearchParams()
        items, total, next_token = await svc.search_projects("t1", params)
        assert items == []
        assert total == 0
        assert next_token is None

    @pytest.mark.asyncio
    async def test_owner_filter_bound(self) -> None:
        svc, _, fetch_all = self._make_service()
        params = ProjectSearchParams(owner_id="owner-42")
        await svc.search_projects("t1", params)
        bind = fetch_all.call_args[0][1]
        assert bind["owner_id"] == "owner-42"

    @pytest.mark.asyncio
    async def test_sort_direction_reflected_in_sql(self) -> None:
        svc, _, fetch_all = self._make_service()
        params = ProjectSearchParams(sort_by="name", sort_dir=SortDirection.asc)
        await svc.search_projects("t1", params)
        sql: str = fetch_all.call_args[0][0]
        assert "p.name asc" in sql.lower()
