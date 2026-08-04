"""Elastic registry discovery and stable-version selection."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
import re
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request

from fastapi import HTTPException

from .contracts import version_key


class RegistryListingParser(HTMLParser):
    """Collect stable tags from Elastic's public repository listing."""

    def __init__(self, repository: str):
        super().__init__()
        self.prefix = f"/r/{repository}:"
        self.tags: set[str] = set()

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        href = dict(attrs).get("href", "")
        if href.startswith(self.prefix):
            version = urllib.parse.unquote(href[len(self.prefix):])
            if version_key(version):
                self.tags.add(version)


class ElasticRegistry:
    """Bounded, cached public registry client with injectable transport seams."""

    def __init__(
        self,
        *,
        cache: dict,
        cache_seconds: int,
        request_timeout: int,
        listing_timeout: int,
        tag_page_size: int,
        tag_page_limit: int,
        tag_result_limit: int,
        urlopen: Callable = urllib.request.urlopen,
    ):
        self._cache = cache
        self._cache_seconds = cache_seconds
        self._request_timeout = request_timeout
        self._listing_timeout = listing_timeout
        self._tag_page_size = tag_page_size
        self._tag_page_limit = tag_page_limit
        self._tag_result_limit = tag_result_limit
        self._urlopen = urlopen

    def json(self, url: str, headers=None):
        request = urllib.request.Request(url, headers=headers or {"Accept": "application/json"})
        try:
            with self._urlopen(request, timeout=self._request_timeout) as response:
                return json.loads(response.read().decode()), response.headers
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
            challenge = error.headers.get("WWW-Authenticate", "")
            if not challenge.lower().startswith("bearer "):
                raise
            parts = dict(re.findall(r'([a-zA-Z_]+)="([^"]+)"', challenge))
            realm = parts.get("realm")
            if not realm:
                raise
            query = urllib.parse.urlencode({key: value for key, value in parts.items() if key in {"service", "scope"}})
            token_payload, _ = self.json(realm + ("?" + query if query else ""))
            token = token_payload.get("token") or token_payload.get("access_token")
            if not token:
                raise HTTPException(503, "Elastic registry did not return an access token")
            authenticated = urllib.request.Request(
                url, headers={"Accept": "application/json", "Authorization": "Bearer " + token}
            )
            with self._urlopen(authenticated, timeout=self._request_timeout) as response:
                return json.loads(response.read().decode()), response.headers

    def tags(self, repository: str, cursor: str, *, fetch_json: Callable | None = None) -> set[str]:
        cache_key = (repository, cursor)
        cached = self._cache.get(cache_key)
        if cached and cached[0] + self._cache_seconds > time.time():
            return cached[1]
        url = f"https://docker.elastic.co/v2/{repository}/tags/list?n={self._tag_page_size}"
        if cursor:
            url += "&last=" + urllib.parse.quote(cursor, safe="")
        tags: set[str] = set()
        error = None
        fetch = fetch_json or self.json
        for _ in range(self._tag_page_limit):
            for attempt in range(3):
                try:
                    payload, headers = fetch(url)
                    break
                except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as caught:
                    error = caught
                    if attempt < 2:
                        time.sleep(0.5 * (attempt + 1))
            else:
                raise HTTPException(503, "Unable to retrieve Elastic image versions") from error
            tags.update(tag for tag in payload.get("tags", []) if version_key(tag))
            if len(tags) >= self._tag_result_limit:
                break
            match = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", ""))
            if not match:
                break
            url = urllib.parse.urljoin("https://docker.elastic.co", match.group(1))
        if not tags:
            raise HTTPException(503, f"Elastic registry returned no stable versions for {repository}")
        self._cache[cache_key] = (time.time(), tags)
        return tags

    def listing_tags(self, repository: str) -> set[str]:
        cache_key = ("listing", repository)
        cached = self._cache.get(cache_key)
        if cached and cached[0] + self._cache_seconds > time.time():
            return cached[1]
        url = f"https://www.docker.elastic.co/r/{repository}?limit=1000&offset=0&show_snapshots=false"
        try:
            request = urllib.request.Request(url, headers={"Accept": "text/html"})
            with self._urlopen(request, timeout=self._listing_timeout) as response:
                parser = RegistryListingParser(repository)
                parser.feed(response.read().decode())
        except (OSError, UnicodeDecodeError, urllib.error.URLError, urllib.error.HTTPError) as error:
            raise HTTPException(503, "Unable to retrieve Elastic image versions") from error
        if not parser.tags:
            raise HTTPException(503, f"Elastic registry returned no stable versions for {repository}")
        self._cache[cache_key] = (time.time(), parser.tags)
        return parser.tags


def repositories(assignments, *, role_images: dict, metricbeat_roles: frozenset, metricbeat_image: str, filebeat_enabled=False, filebeat_image: str = "") -> set[str]:
    values = {role_images[assignment["role"]] for assignment in assignments if assignment["role"] in role_images}
    if any(assignment["role"] in metricbeat_roles for assignment in assignments):
        values.add(metricbeat_image)
    if filebeat_enabled and assignments:
        values.add(filebeat_image)
    return values


def version_cursor(assignments, default_version: str) -> str:
    known = [
        version_key((assignment.get("observation") or {}).get("version", ""))
        or version_key(assignment.get("image_version", ""))
        or version_key(assignment.get("desired_version", ""))
        for assignment in assignments
    ]
    versions = [value for value in known if value]
    minimum = min(versions) if versions else version_key(default_version)
    return f"{minimum[0]}.{max(minimum[1] - 1, 0)}.999"


def available_role_versions(role, assignments, *, role_images: dict, default_version: str, listing_tags: Callable, limit: int) -> list[str]:
    repository = role_images.get(role)
    if not repository:
        raise HTTPException(422, "Unknown workload role")
    minimum = version_key(version_cursor(assignments, default_version))
    tags = listing_tags(repository)
    values = sorted((tag for tag in tags if version_key(tag) > minimum), key=version_key, reverse=True)[:limit]
    configured = {
        version
        for assignment in assignments
        for version in (
            (assignment.get("observation") or {}).get("version", ""), assignment.get("image_version", ""), assignment.get("desired_version", ""),
        )
        if version_key(version) and version in tags and version_key(version) > minimum
    }
    return sorted(set(values).union(configured), key=version_key, reverse=True)


def available_versions(assignments, *, role_images: dict, metricbeat_roles: frozenset, metricbeat_image: str, filebeat_image: str, default_version: str, registry_tags: Callable, result_limit: int, filebeat_enabled=False) -> list[str]:
    values = repositories(assignments, role_images=role_images, metricbeat_roles=metricbeat_roles, metricbeat_image=metricbeat_image, filebeat_enabled=filebeat_enabled, filebeat_image=filebeat_image)
    if not values:
        return []
    cursor = version_cursor(assignments, default_version)
    with ThreadPoolExecutor(max_workers=min(4, len(values))) as executor:
        groups = list(executor.map(lambda repository: registry_tags(repository, cursor), sorted(values)))
    common = None
    for tags in groups:
        common = tags if common is None else common.intersection(tags)
    return sorted(common or (), key=version_key, reverse=True)[:result_limit]


def recommended_version(assignments, candidates) -> str:
    running = [
        (assignment.get("observation") or {}).get("version", "")
        for assignment in assignments
        if (assignment.get("observation") or {}).get("running") and version_key((assignment.get("observation") or {}).get("version", ""))
    ]
    configured = [
        assignment.get("image_version", "") or assignment.get("desired_version", "")
        for assignment in assignments
        if version_key(assignment.get("image_version", "") or assignment.get("desired_version", ""))
    ]
    current = running or configured
    if current:
        counts = {version: current.count(version) for version in set(current)}
        selected = max(counts, key=lambda version: (counts[version], version_key(version)))
        if not candidates or selected in candidates:
            return selected
    return candidates[0] if candidates else ""
