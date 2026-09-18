"""Models over the estate layers.

Kept out of gis/layers.py deliberately. That module is a pure join: it imports
cheaply, never fits anything, and serves the map on every request. Anything
here may fit a model, so it caches its result and is never called inside a
request path without one.

Every model in this package answers to the same two rules the rest of the
build follows:

  * it declares what it ran on, in the provenance vocabulary the UI badges
  * it returns every figure its narrative will quote, pre-computed, because
    gis/reasoning.audit_figures rejects arithmetic a model performed on the
    way to a sentence
"""
