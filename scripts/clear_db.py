"""Clear all records from the database. Schema is preserved."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.db import get_session
from src.storage.models import (
    SimSession, SimPosition, ArbSimulation,
    Recommendation, Outcome, EvaluationReport,
)

def main():
    answer = input("Clear all DB records? This cannot be undone. [y/N] ").strip().lower()
    if answer != "y":
        print("Aborted.")
        return

    db = get_session()
    counts = {}
    for model in [SimPosition, SimSession, ArbSimulation, Recommendation, Outcome, EvaluationReport]:
        n = db.query(model).delete()
        counts[model.__tablename__] = n
    db.commit()

    for table, n in counts.items():
        if n:
            print(f"  Deleted {n:>4} rows from {table}")
    print("Done.")

if __name__ == "__main__":
    main()
