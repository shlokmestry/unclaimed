# Step 6 — FastAPI backend.
#
# Serves the uncovered_agencies table built by the ingestion pipeline
# (run.py / ingestion/*). Read-only: this API never writes to the database.

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, select

from db.async_session import AsyncSessionLocal
from db.models import UncoveredAgency

app = FastAPI(title="Unclaimed API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # This API is fully public and read-only with no cookies/auth, so there's
    # no session to carry — allow_credentials=True combined with a wildcard
    # origin is an invalid combination per the CORS spec anyway (browsers
    # reject it for credentialed requests).
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Whitelisted sort columns — sort_by is a query param, so we map it to an
# actual ORM column ourselves rather than building an order_by clause from
# raw user input.
SORTABLE_COLUMNS = {
    "opportunity_score": UncoveredAgency.opportunity_score,
    "quality_score": UncoveredAgency.quality_score,
    "population": UncoveredAgency.population,
    "name": UncoveredAgency.name,
    "country": UncoveredAgency.country,
    "municipality": UncoveredAgency.municipality,
}

# Valid readiness_status values (Improvement 2) — validated the same way as
# sort_by, against a whitelist, rather than passed straight into the query.
READINESS_VALUES = {"Ready", "Needs Work", "Dead Feed"}


class AgencyOut(BaseModel):
    id: int
    mobility_agency_id: int
    name: Optional[str] = None
    country: Optional[str] = None
    municipality: Optional[str] = None
    feed_url: Optional[str] = None
    population: Optional[int] = None
    quality_score: Optional[float] = None
    opportunity_score: Optional[float] = None
    missing_files: Optional[list] = None

    # --- Opportunity-score rework fields ---
    has_realtime: Optional[bool] = None
    feed_last_updated: Optional[datetime] = None
    route_count: Optional[int] = None
    stop_count: Optional[int] = None
    trip_count: Optional[int] = None
    readiness_status: Optional[str] = None

    model_config = {"from_attributes": True}


class LargestMarket(BaseModel):
    name: Optional[str] = None
    country: Optional[str] = None
    population: Optional[int] = None


class StatsOut(BaseModel):
    total_uncovered: int
    ready_to_onboard: int
    countries_count: int
    realtime_count: int
    avg_quality: Optional[float] = None
    largest_market: Optional[LargestMarket] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/agencies", response_model=list[AgencyOut])
async def list_agencies(
    sort_by: str = Query("opportunity_score", description=f"One of {sorted(SORTABLE_COLUMNS)}"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    country: Optional[str] = Query(None, description="Exact-match country filter"),
    min_quality: Optional[float] = Query(None, ge=0, le=100),
    readiness: Optional[str] = Query(
        None, description=f"One of {sorted(READINESS_VALUES)}"
    ),
):
    column = SORTABLE_COLUMNS.get(sort_by)
    if column is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort_by {sort_by!r}. Must be one of {sorted(SORTABLE_COLUMNS)}",
        )

    if readiness is not None and readiness not in READINESS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid readiness {readiness!r}. Must be one of {sorted(READINESS_VALUES)}",
        )

    order_clause = column.desc() if order == "desc" else column.asc()

    stmt = select(UncoveredAgency).order_by(order_clause)
    if country:
        stmt = stmt.where(UncoveredAgency.country == country)
    if min_quality is not None:
        stmt = stmt.where(UncoveredAgency.quality_score >= min_quality)
    if readiness is not None:
        stmt = stmt.where(UncoveredAgency.readiness_status == readiness)

    async with AsyncSessionLocal() as session:
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return rows


@app.get("/agencies/{agency_id}", response_model=AgencyOut)
async def get_agency(agency_id: int):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UncoveredAgency).where(UncoveredAgency.id == agency_id)
        )
        row = result.scalar_one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Agency not found")

    return row


@app.get("/agencies/{agency_id}/rank")
async def get_agency_rank(agency_id: int):
    """Where this agency sits in the full opportunity_score ranking (e.g.
    "#4 of 2,939") — used by the frontend's agency detail view (Improvement
    3's "Rank among all uncovered agencies"). A dedicated endpoint rather
    than making the frontend fetch and rank all 2,939 rows itself just to
    find one agency's position."""

    async with AsyncSessionLocal() as session:
        agency = await session.get(UncoveredAgency, agency_id)
        if agency is None:
            raise HTTPException(status_code=404, detail="Agency not found")

        total = await session.scalar(select(func.count()).select_from(UncoveredAgency))

        if agency.opportunity_score is None:
            rank = None
        else:
            higher_count = await session.scalar(
                select(func.count())
                .select_from(UncoveredAgency)
                .where(UncoveredAgency.opportunity_score > agency.opportunity_score)
            )
            rank = higher_count + 1

    return {"rank": rank, "total": total}


@app.get("/stats", response_model=StatsOut)
async def stats():
    async with AsyncSessionLocal() as session:
        total = await session.scalar(select(func.count()).select_from(UncoveredAgency))
        ready_to_onboard = await session.scalar(
            select(func.count())
            .select_from(UncoveredAgency)
            .where(UncoveredAgency.readiness_status == "Ready")
        )
        countries_count = await session.scalar(
            select(func.count(func.distinct(UncoveredAgency.country)))
        )
        realtime_count = await session.scalar(
            select(func.count())
            .select_from(UncoveredAgency)
            .where(UncoveredAgency.has_realtime.is_(True))
        )
        avg_quality = await session.scalar(select(func.avg(UncoveredAgency.quality_score)))

        largest_result = await session.execute(
            select(UncoveredAgency.name, UncoveredAgency.country, UncoveredAgency.population)
            .where(UncoveredAgency.population.is_not(None))
            .order_by(UncoveredAgency.population.desc())
            .limit(1)
        )
        largest_row = largest_result.first()
        largest_market = (
            LargestMarket(name=largest_row[0], country=largest_row[1], population=largest_row[2])
            if largest_row
            else None
        )

    return StatsOut(
        total_uncovered=total or 0,
        ready_to_onboard=ready_to_onboard or 0,
        countries_count=countries_count or 0,
        realtime_count=realtime_count or 0,
        avg_quality=round(avg_quality, 2) if avg_quality is not None else None,
        largest_market=largest_market,
    )
