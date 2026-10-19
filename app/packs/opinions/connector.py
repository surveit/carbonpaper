"""courtlistener_opinions: the opinions a CourtListener search finds, one row per stored PDF."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import Annotated, BinaryIO, Literal, Self
from urllib.parse import urlencode

from pydantic import (
    BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator,
)

from app.core.errors import SourceUnavailable
from app.core.url_fetch import open_url, read_url
from app.models.connectors import AcquiredBytes, ConnectorParams, ConnectorSpec, MetadataValue
from app.models.schema import Column

COURTLISTENER_API = "https://www.courtlistener.com/api/rest/v4/"
OPINION_STORAGE = "https://storage.courtlistener.com/"
USER_AGENT = "carbonpaper (https://github.com/surveit/carbonpaper)"
_COURTLISTENER_SITE = "https://www.courtlistener.com"
_TIMEOUT_S = 60

CourtId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+$")]


class CourtListenerOpinionsParams(ConnectorParams):
    query: str = Field(min_length=1)
    courts: list[CourtId] = Field(min_length=1)
    filed_after: date
    filed_before: date
    statuses: list[Literal["Published", "Unpublished"]] = Field(min_length=1)
    # The CourtListener clusters (one decision each) to read, each of which the search must return.
    cluster_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1)

    @model_validator(mode="after")
    def _refuse_reversed_dates(self) -> Self:
        if self.filed_after > self.filed_before:
            raise ValueError(
                f"filed_after {self.filed_after} is after filed_before {self.filed_before}")
        return self

    @model_validator(mode="after")
    def _names_each_cluster_once(self) -> Self:
        ids = self.cluster_ids
        repeated = sorted({cluster_id for cluster_id in ids if ids.count(cluster_id) > 1})
        if repeated:
            raise ValueError(f"cluster_ids names {repeated} more than once")
        return self


def acquire_courtlistener_opinions(params: CourtListenerOpinionsParams) -> Iterator[AcquiredBytes]:
    listing = _read_listing(params)
    for cluster, stored in _find_requested_clusters(listing, params):
        yield _acquire_cluster(cluster, stored)


class _Opinion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    local_path: str | None
    download_url: str | None


class _Cluster(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cluster_id: int
    court_id: str
    case_name: str = Field(validation_alias="caseName")
    date_filed: date = Field(validation_alias="dateFiled")
    docket_number: str | None = Field(validation_alias="docketNumber")
    status: str
    nature_of_suit: str | None = Field(validation_alias="suitNature")
    absolute_url: str | None
    opinions: list[_Opinion]


class _SearchPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    next: str | None
    results: list[_Cluster]


@dataclass(frozen=True)
class _StoredOpinion:
    opinion_id: int
    local_path: str


def _read_listing(params: CourtListenerOpinionsParams) -> dict[int, _Cluster]:
    url: str | None = _build_search_url(params)
    results: list[_Cluster] = []
    while url is not None:
        page = _read_search_page(url)
        results.extend(page.results)
        url = page.next
        if url is not None and not url.startswith(COURTLISTENER_API):
            raise SourceUnavailable(
                f"the search for {params.query!r} points its next page at {url}, off "
                f"{COURTLISTENER_API}; it is not followed")
    return _index_by_cluster(results)


def _build_search_url(params: CourtListenerOpinionsParams) -> str:
    clusters = " OR ".join(str(cluster_id) for cluster_id in params.cluster_ids)
    query = f"({params.query}) AND cluster_id:({clusters})"
    return f"{COURTLISTENER_API}search/?" + urlencode([
        ("type", "o"), ("q", query), ("court", " ".join(params.courts)),
        ("filed_after", params.filed_after.isoformat()),
        ("filed_before", params.filed_before.isoformat()),
        *[(f"stat_{status}", "on") for status in params.statuses]])


def _read_search_page(url: str) -> _SearchPage:
    body = read_url(url, user_agent=USER_AGENT, timeout_s=_TIMEOUT_S)
    try:
        return _SearchPage.model_validate_json(body)
    except ValidationError as invalid:
        raise SourceUnavailable(
            f"{url} is not a CourtListener opinion search page: {invalid}") from invalid


def _index_by_cluster(results: list[_Cluster]) -> dict[int, _Cluster]:
    """The cursor repeats results; a repeat that names other PDFs than its first copy refuses."""
    indexed: dict[int, _Cluster] = {}
    for cluster in results:
        first_paths = _find_stored_paths(indexed.setdefault(cluster.cluster_id, cluster))
        paths = _find_stored_paths(cluster)
        if paths != first_paths:
            raise SourceUnavailable(
                f"cluster {cluster.cluster_id} appears twice in the search with different "
                f"stored PDFs: {sorted(first_paths)} and {sorted(paths)}")
    return indexed


def _find_stored_paths(cluster: _Cluster) -> set[str]:
    return {opinion.local_path for opinion in cluster.opinions if opinion.local_path}


def _find_requested_clusters(
    listing: dict[int, _Cluster], params: CourtListenerOpinionsParams,
) -> list[tuple[_Cluster, _StoredOpinion]]:
    found: list[tuple[_Cluster, _StoredOpinion]] = []
    problems: list[str] = []
    for cluster_id in params.cluster_ids:
        cluster = listing.get(cluster_id)
        if cluster is None:
            problems.append(f"cluster {cluster_id} is not among the search's results: it does not "
                            "match the query, courts, dates or statuses")
            continue
        stored = _find_stored_opinions(cluster)
        problems.extend(_find_cluster_problems(cluster, stored))
        if len(stored) == 1:
            found.append((cluster, stored[0]))
    if problems:
        raise SourceUnavailable("; ".join(problems))
    return found


def _find_cluster_problems(cluster: _Cluster, stored: list[_StoredOpinion]) -> list[str]:
    problems: list[str] = []
    if len(stored) != 1:
        problems.append(_name_stored_problem(cluster.cluster_id, stored))
    link = cluster.absolute_url
    if link is not None and not link.startswith("/"):
        problems.append(f"cluster {cluster.cluster_id} has absolute_url {link!r}, which is not "
                        f"a path on {_COURTLISTENER_SITE}")
    return problems


def _name_stored_problem(cluster_id: int, stored: list[_StoredOpinion]) -> str:
    if not stored:
        return (f"cluster {cluster_id} has no stored PDF: no opinion carries a local_path; "
                "take it out of params.cluster_ids")
    return (f"cluster {cluster_id} has {len(stored)} stored PDFs, not one: "
            f"{[opinion.local_path for opinion in stored]}")


def _find_stored_opinions(cluster: _Cluster) -> list[_StoredOpinion]:
    """One entry per distinct stored PDF, naming the first opinion that holds it."""
    stored: dict[str, _StoredOpinion] = {}
    for opinion in cluster.opinions:
        if opinion.local_path:
            stored.setdefault(opinion.local_path, _StoredOpinion(opinion.id, opinion.local_path))
    return list(stored.values())


def _acquire_cluster(cluster: _Cluster, stored: _StoredOpinion) -> AcquiredBytes:
    origin_url = f"{OPINION_STORAGE}{stored.local_path}"
    return AcquiredBytes(
        f"{cluster.court_id}_cluster{cluster.cluster_id}.pdf", origin_url,
        _bind_opener(origin_url), _read_metadata(cluster, stored))


def _bind_opener(url: str) -> Callable[[], BinaryIO]:
    return partial(open_url, url, user_agent=USER_AGENT, timeout_s=_TIMEOUT_S)


def _read_metadata(cluster: _Cluster, stored: _StoredOpinion) -> dict[str, MetadataValue]:
    page_url = f"{_COURTLISTENER_SITE}{cluster.absolute_url}" if cluster.absolute_url else None
    return {"cluster_id": cluster.cluster_id, "opinion_id": stored.opinion_id,
            "court_id": cluster.court_id, "case_name": cluster.case_name,
            "date_filed": cluster.date_filed, "precedential_status": cluster.status,
            "docket_number": cluster.docket_number, "nature_of_suit": cluster.nature_of_suit,
            "courtlistener_url": page_url}


_METADATA_COLUMNS = (
    Column(name="cluster_id", type="int", nullable=True),
    Column(name="opinion_id", type="int", nullable=True),
    Column(name="court_id", type="str", nullable=True),
    Column(name="case_name", type="str", nullable=True),
    Column(name="date_filed", type="date", nullable=True),
    Column(name="precedential_status", type="str", nullable=True),
    Column(name="docket_number", type="str", nullable=True),
    Column(name="nature_of_suit", type="str", nullable=True),
    Column(name="courtlistener_url", type="str", nullable=True),
)

COURTLISTENER_OPINIONS = ConnectorSpec(
    kind="courtlistener_opinions", params_model=CourtListenerOpinionsParams,
    metadata_columns=_METADATA_COLUMNS, acquire=acquire_courtlistener_opinions)
