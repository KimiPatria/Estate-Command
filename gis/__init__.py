"""Estate Command — the GIS decision layer.

Geometry lives as GeoJSON on disk (gis/data/), not in PostGIS. The EPMS
databases are read by build scripts only (gis/build_estate_data.py snapshots
each estate's blocks and harvest) and PostGIS is not installed on this
Postgres, so every spatial artefact here is built once by a script and then
served as a static file. This mirrors forecast/blocks.py, which
already serves real block polygons the same way.

Two kinds of geometry, never mixed silently:
  * REAL      — EC, EA and EB block polygons, the client's ArcGIS export as
                held in each estate's EPMS m_overlay.
  * SYNTHETIC — everything the source systems do not record: mills, vendors,
                fire posts, roads, and the block tessellation for estates
                that have no shapefile yet.

Every feature carries a `provenance` property saying which it is. The demo
puts that on screen rather than hiding it (see the readiness panel, UC-15).
"""
