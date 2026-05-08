"""
clients/data/

Data acquisition + persistence library. Each source is one module exposing a
narrow class API plus a CLI `main()` entry point so existing shell scripts
that do `python -m clients.data.X ...` keep working.

  wu                       Weather Underground half-hourly observations.
  open_meteo               Open-Meteo ERA5 archive + GFS forecast.
  polymarket_meta          Polymarket gamma-api event/market metadata.
  polymarket_prices        Polymarket CLOB 1-minute price history.
  polymarket_orderbook     Polymarket CLOB live L2 orderbook collector.
  polymarket_search        Ad-hoc gamma-api keyword search (utility, optional).

The previous `core/` package was renamed to this; older import paths still
work via the thin shims at the original locations (until those are removed).
"""

# We keep the public surface lazy — strategy / dashboard code typically only
# needs ONE source at a time, and several modules pull in heavy deps
# (lightgbm, aiohttp, pyarrow). Importing them eagerly slows every script.
__all__ = [
    "wu",
    "open_meteo",
    "polymarket_meta",
    "polymarket_prices",
    "polymarket_orderbook",
    "polymarket_search",
]
