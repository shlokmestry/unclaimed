# Outreach Email — Unclaimed

## Subject line options

1. Transit agencies with public GTFS feeds Transit doesn't cover yet
2. A ranked list of transit agencies you're not covering yet
3. 1,117 transit agencies ready to onboard today

Pick one before sending — option 1 is the safest default since it doesn't depend on a figure being finalized.

---

## Email body

Subject: Transit agencies with public GTFS feeds Transit doesn't cover yet

Hi {RECIPIENT_NAME},

I built a small tool called Unclaimed that cross-references every public GTFS feed in the Mobility Database against Transit's published coverage (transitapp.com/region) to find agencies with an available feed that Transit doesn't serve yet. Each one gets an opportunity score blending country population, feed quality, whether it has a realtime (GTFS-RT) feed, how recently the feed was updated, and network size — plus a readiness classification (Ready / Needs Work / Dead Feed) based on feed completeness, freshness, and reachability.

Right now it's showing 2,939 uncovered agencies, of which 1,117 are marked Ready — clean, recently updated feeds, several with realtime data already available. The top of the list is dominated by large, well-known US transit authorities (MBTA, SEPTA, RTD, AC Transit) that are fully absent from Transit's coverage despite mature, high-quality public data.

Some caveats worth being upfront about: coverage matching is fuzzy-string-based (name + country code), so there will be some false positives/negatives at the margins, and population is a market-size proxy, not a demand estimate. I'd treat this as a prioritization starting point, not ground truth.

I put together a one-page summary built for exactly this: https://unclaimed-five.vercel.app/?view=pitch (prints cleanly to PDF too). Full interactive dashboard, with search/filter/sort and a detail page per agency, is at https://unclaimed-five.vercel.app. Happy to share the raw data or the matching logic if useful.

{YOUR_NAME}
