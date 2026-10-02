"""A RECORD field above csv.field_size_limit crashes the guard."""
import sys
from pathlib import Path

from scripts.check_import_origins import check_origins, _site_packages_roots

site = next(root for root in _site_packages_roots() if str(root).startswith(str(Path(sys.prefix))))
record_dir = site / "review_oversize-1.0.dist-info"
record_dir.mkdir()
record = record_dir / "RECORD"
record.write_text("x" * 150000 + ",sha256=abc,1\n", encoding="utf-8")
try:
    print("csv field size limit exceeded by planted RECORD")
    try:
        result = check_origins(Path(__file__).parent / "tree", ("pokered_harness",))
    except BaseException as exc:
        print("escaped:", type(exc).__name__, str(exc))
    else:
        print("guard status:", result["status"])
finally:
    record.unlink()
    record_dir.rmdir()
