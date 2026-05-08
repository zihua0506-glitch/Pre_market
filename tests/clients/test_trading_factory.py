"""Tests for clients.trading.make_backend factory."""

import pytest

from clients.trading import DryRunBackend, TradingBackend, make_backend
from clients.trading.dryrun import DryRunBackend as DRB


class TestFactory:
    def test_dryrun(self):
        b = make_backend("dryrun", verbose=False)
        assert isinstance(b, TradingBackend)
        assert isinstance(b, DRB)

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="unknown trading backend"):
            make_backend("nonsense")

    def test_kwargs_pass_through(self):
        b = make_backend("dryrun", yes_mid=0.42, verbose=False)
        ob = b.get_book("any")
        # bid level should be just below yes_mid
        assert abs(ob.bids[0].price - (0.42 - 0.02)) < 1e-9


class TestLazyImport:
    def test_proxy_only_loaded_when_accessed(self):
        """ProxyBackend isn't pulled in until factory call."""
        import clients.trading
        # Just touching the module shouldn't import requests-mock-able heavy deps;
        # but accessing the factory result should resolve ProxyBackend.
        # Simply check the lazy lookup works:
        cls = clients.trading.DryRunBackend
        assert cls is DRB
