"""
ops/

Operational tooling — wallet bootstrap, CLOB credential derivation, on-chain
proxy deployment, preflight checks. Imported as a regular package so its
functions can be reused (e.g. by a dashboard's "rotate wallet" button), and
still runnable as `python -m ops.<name>` for one-off CLI use.

Modules:
  setup_clob_creds   Derive CLOB API key/secret/passphrase from POLY_PRIVATE_KEY
                     and write them to .env. One-time at first deploy or after
                     wallet rotation.
  rotate_wallet      Generate a fresh EOA, push new .env to remote hosts.
                     Emergency-only.
  deploy_proxy       Call Polymarket ProxyWalletFactory.proxy([]) on-chain to
                     deploy the wallet's Proxy contract. Use only when the
                     UI deposit page is unreachable (datacenter IP).
  preflight_clob     IP geo / ban-status check + balance/allowance + place &
                     cancel a small test order. Run before going live.

Each module exports `main(argv)` returning an int exit code; the
`scripts/<name>.py` shim is just a thin argparse delegate.
"""
