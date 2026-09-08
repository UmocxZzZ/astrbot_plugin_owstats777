from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

try:
    from overstats.src.client.apiclient import DashenAPIClient, dashen_api_client
except ModuleNotFoundError:
    from src.client.apiclient import DashenAPIClient, dashen_api_client

from ..errors import ModuleError
from ..query_tool.requests import QueryToolRequests


@dataclass(frozen=True)
class BnetSearchResult:
    query: str
    payload: Dict[str, Any]

    @property
    def data(self) -> Dict[str, Any]:
        data = self.payload.get("data")
        return data if isinstance(data, dict) else {}

    @property
    def customer_token(self) -> str:
        return str(self.data.get("customerToken") or "").strip()

    @property
    def bnet_id(self) -> str:
        return str(self.data.get("bnetId") or "").strip()

    @property
    def full_id(self) -> str:
        return str(self.data.get("name") or self.query).strip()

    @property
    def icon_url(self) -> str:
        return str(self.data.get("icon") or "").strip()


def normalize_bnet_id(bnet_id: str) -> str:
    return str(bnet_id or "").replace("＃", "#").strip()


class BnetSearchRequests:
    def __init__(
        self,
        api_client: Optional[DashenAPIClient] = None,
        query_tool_requests: Optional[QueryToolRequests] = None,
    ) -> None:
        self.api_client = api_client or dashen_api_client
        self.query_tool_requests = query_tool_requests or QueryToolRequests()

    async def search(self, bnet_id: str) -> BnetSearchResult:
        query = normalize_bnet_id(bnet_id)
        try:
            payload = await self.api_client.search_bnet_account(query)
        except httpx.RequestError:
            await self._raise_if_search_maintenance()
            raise
        result = BnetSearchResult(query=query, payload=payload)
        if result.customer_token:
            return result

        await self._raise_if_search_maintenance()
        return result

    async def _raise_if_search_maintenance(self) -> None:
        notice = await self.query_tool_requests.fetch_search_maintenance_notice()
        if notice:
            raise ModuleError(
                error="dashen_search_maintenance",
                message="NetEase Dashen search interface is under maintenance.",
                status_code=503,
                details={"official_notice": notice},
            )
