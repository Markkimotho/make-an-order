"""The demo seed (scripts/demo.py) runs end to end and never puts a
prescription medicine in the pharmacy's online shop."""
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_demo_seed_runs_and_prescription_medicines_stay_off_the_shop(tmp_path):
    db = tmp_path / "demo.sqlite"
    r = subprocess.run([sys.executable, "scripts/demo.py", "seed", "--db", str(db)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT i.name, i.requires_prescription FROM inventory_items i "
        "JOIN tenants t ON t.id = i.tenant_id JOIN categories c ON c.id = i.category_id "
        "WHERE t.slug = 'nuru' AND c.name IN ('Antibiotics', 'Chronic care')").fetchall()
    assert rows and all(rx for _, rx in rows), rows
    # The chronic patient's medicines were dispensed by the pharmacist.
    dispensed = con.execute("SELECT COUNT(*) FROM prescription_items "
                            "WHERE quantity_dispensed > 0").fetchone()[0]
    assert dispensed >= 2
