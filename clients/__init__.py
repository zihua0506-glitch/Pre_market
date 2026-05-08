"""
clients/

Reusable client libraries for ZSPD Polymarket trader. Two sub-packages:

  clients.data      Market-data acquisition + persistence (WU, Open-Meteo,
                    Polymarket meta, prices, order book history).

  clients.trading   Order book queries + order placement / cancellation.
                    Multiple backends: direct (py-clob-client-v2 native),
                    proxy (HTTP through clob-proxy-server), dryrun.

Both layers expose narrow class-based APIs intended to be importable from any
strategy, dashboard, monitor, or one-off script — not coupled to modify_str.
"""
