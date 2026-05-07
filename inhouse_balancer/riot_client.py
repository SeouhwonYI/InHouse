"""Small Riot API client for future integration.

The Streamlit MVP works without Riot API. This client is intentionally thin and only
contains enough to fetch Riot ID -> PUUID, summoner profile, ranked entries, and recent
matches when RIOT_API_KEY is provided.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests


class RiotAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class RiotClient:
    api_key: str | None = None
    platform_host: str = "kr.api.riotgames.com"
    regional_host: str = "asia.api.riotgames.com"
    timeout: float = 10.0
    min_interval_seconds: float = 1.25
    _last_request_ts: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = os.getenv("RIOT_API_KEY")
        self._last_request_ts = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _get(self, host: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.api_key:
            raise RiotAPIError("RIOT_API_KEY is not set.")

        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self.min_interval_seconds:
            time.sleep(self.min_interval_seconds - elapsed)

        url = f"https://{host}{path}"
        response = requests.get(
            url,
            params=params or {},
            headers={"X-Riot-Token": self.api_key},
            timeout=self.timeout,
        )
        self._last_request_ts = time.monotonic()

        if response.status_code >= 400:
            raise RiotAPIError(f"{response.status_code} {response.reason}: {response.text[:300]}")
        return response.json()

    def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict[str, Any]:
        game_name_q = quote(game_name, safe="")
        tag_line_q = quote(tag_line, safe="")
        return self._get(
            self.regional_host,
            f"/riot/account/v1/accounts/by-riot-id/{game_name_q}/{tag_line_q}",
        )

    def get_account_by_puuid(self, puuid: str) -> dict[str, Any]:
        return self._get(self.regional_host, f"/riot/account/v1/accounts/by-puuid/{puuid}")

    def get_summoner_by_puuid(self, puuid: str) -> dict[str, Any]:
        return self._get(self.platform_host, f"/lol/summoner/v4/summoners/by-puuid/{puuid}")

    def get_rank_entries_by_puuid(self, puuid: str) -> list[dict[str, Any]]:
        return self._get(self.platform_host, f"/lol/league/v4/entries/by-puuid/{puuid}")

    def get_recent_match_ids(self, puuid: str, *, start: int = 0, count: int = 20) -> list[str]:
        return self._get(
            self.regional_host,
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids",
            params={"start": start, "count": count},
        )

    def get_match(self, match_id: str) -> dict[str, Any]:
        return self._get(self.regional_host, f"/lol/match/v5/matches/{match_id}")

    def get_solo_queue_entry(self, riot_game_name: str, riot_tag_line: str) -> dict[str, Any] | None:
        account = self.get_account_by_riot_id(riot_game_name, riot_tag_line)
        entries = self.get_rank_entries_by_puuid(account["puuid"])
        for entry in entries:
            if entry.get("queueType") == "RANKED_SOLO_5x5":
                return entry
        return None
