"""scripts/rotate_wallet.py — thin shim. Logic in ops.rotate_wallet."""
import sys
from ops.rotate_wallet import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
