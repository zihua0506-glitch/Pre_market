"""Smoke-import tests for clients.data.

The clients.data modules are heavyweight (sqlite + http + pandas + aiohttp)
and previously lived under core/. After Phase 3b they were renamed; these
tests just confirm each new module path imports cleanly so future renames /
typos don't go unnoticed.

Behaviour-level data-fetching tests would require either a network or a
fixture-cassette infra; those are deferred. Marked here as TODO comments.
"""

import importlib

import pytest


@pytest.mark.parametrize("module", [
    "clients.data",
    "clients.data.wu",
    "clients.data.open_meteo",
    "clients.data.polymarket_meta",
    "clients.data.polymarket_prices",
    "clients.data.polymarket_orderbook",
    "clients.data.polymarket_search",
])
def test_module_imports_cleanly(module: str):
    importlib.import_module(module)


def test_polymarket_prices_view_migration_helper_present():
    """The view-migration helper added in earlier bug-fix work is still exported."""
    from clients.data.polymarket_prices import init_db, _migrate_token_meta_pk
    assert callable(init_db)
    assert callable(_migrate_token_meta_pk)


def test_wu_fetch_helpers_present():
    """fetch_wu_latest depends on these names from clients.data.wu — keep them."""
    from clients.data.wu import init_db, fetch_day, to_row, INSERT_SQL
    assert callable(init_db)
    assert callable(fetch_day)
    assert callable(to_row)
    assert isinstance(INSERT_SQL, str)


def test_polymarket_meta_url_constant():
    from clients.data.polymarket_meta import GAMMA_API
    assert GAMMA_API == "https://gamma-api.polymarket.com"


def test_polymarket_prices_url_constants():
    from clients.data.polymarket_prices import CLOB_API
    assert CLOB_API == "https://clob.polymarket.com"


# TODO(network-tier): add cassette-based tests for the HTTP fetchers.
# Each module's main() function does a full crawl, but we want narrower
# unit-level tests on things like:
#   - prices history pagination edge cases
#   - polymarket_meta question-title parser
#   - open_meteo backfill date range builder
#   - wu rate-limit retry behaviour
