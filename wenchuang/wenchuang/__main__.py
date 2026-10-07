from .api import run
import sys
run(db_path=sys.argv[1] if len(sys.argv) > 1 else "wcindex.db",
    port=int(sys.argv[2]) if len(sys.argv) > 2 else 8080)
