"""scripts/deploy_proxy.py — thin shim. Logic in ops.deploy_proxy."""
import sys
from ops.deploy_proxy import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
