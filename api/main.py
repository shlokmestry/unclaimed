# Step 6 — FastAPI backend.
#
# Serves the uncovered_agencies table built by the ingestion pipeline
# (run.py / ingestion/*). Read-only: this API never writes to the database.

from __future__ import annotations

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
    allow_credentials=True,
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

    model_config = {"from_attributes": True}


class CountryCount(BaseModel):
    country: Optional[str]
    count: int


class StatsOut(BaseModel):
    total_uncovered: int
    countries_count: int
    avg_quality: Optional[float] = None
    top_5_countries: list[CountryCount]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/agencies", response_model=list[AgencyOut])
async def list_agencies(
    sort_by: str = Query("opportunity_score", description=f"One of {sorted(SORTABLE_COLUMNS)}"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    country: Optional[str] = Query(None, description="Exact-match country filter"),
    min_quality: Optional[float] = Query(None, ge=0, le=100),
):
    column = SORTABLE_COLUMNS.get(sort_by)
    if column is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid sort_by {sort_by!r}. Must be one of {sorted(SORTABLE_COLUMNS)}",
        )

    order_clause = column.desc() if order == "desc" else column.asc()

    stmt = select(UncoveredAgency).order_by(order_clause)
    if country:
        stmt = stmt.where(UncoveredAgency.country == country)
    if min_quality is not None:
        stmt = stmt.where(UncoveredAgency.quality_score >= min_quality)

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


@app.get("/stats", response_model=StatsOut)
async def stats():
    async with AsyncSessionLocal() as session:
        total = await session.scalar(select(func.count()).select_from(UncoveredAgency))
        countries_count = await session.scalar(
            select(func.count(func.distinct(UncoveredAgency.country)))
        )
        avg_quality = await session.scalar(select(func.avg(UncoveredAgency.quality_score)))

        top_result = await session.execute(
            select(UncoveredAgency.country, func.count().label("count"))
            .group_by(UncoveredAgency.country)
            .order_by(func.count().desc())
            .limit(5)
        )
        top_countries = [
            CountryCount(country=row[0], count=row[1]) for row in top_result.all()
        ]

    return StatsOut(
        total_uncovered=total or 0,
        countries_count=countries_count or 0,
        avg_quality=round(avg_quality, 2) if avg_quality is not None else None,
        top_5_countries=top_countries,
    )
