# Outreach Email — Unclaimed

## Subject line options

1. Transit agencies with public GTFS feeds Transit doesn't cover yet
2. A ranked list of transit agencies you're not covering yet
3. 2,939 uncovered agencies, ranked by opportunity

Pick one before sending — option 1 is the safest default since it doesn't depend on a figure being finalized.

---

## Email body

Subject: Transit agencies with public GTFS feeds Transit doesn't cover yet

Hi {RECIPIENT_NAME},

I built a small tool called Unclaimed that cross-references every public GTFS feed in the Mobility Database against Transit's published coverage (transitapp.com/region) to find agencies with an available feed that Transit doesn't serve yet. It ranks the gaps by an opportunity score (60% country population as a market-size proxy, 40% feed quality — required files present, row counts, active service dates), so the biggest, best-documented gaps float to the top.

Right now it's showing 2,939 uncovered agencies, with United States and India contributing the most high-scoring ones — e.g. Bangalore Metropolitan Transport Corporation scores well on both size and feed completeness (India's major-city transit authorities show up repeatedly near the top: large population, complete GTFS feeds, no Transit coverage yet).

Some caveats worth being upfront about: coverage matching is fuzzy-string-based (name + country code), so there will be some false positives/negatives at the margins, and population is a market-size proxy, not a demand estimate. I'd treat it as a prioritization starting point, not ground truth.

Dashboard's here if you want to poke around: {DASHBOARD_URL}. Happy to share the raw data or the matching logic if useful.

{YOUR_NAME}
