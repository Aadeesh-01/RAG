import argparse

from loaders import get_loader
from processing import DataProcessor, rolling_average
from storage import SQLiteStore


def run_pipeline(input_path, db_path=":memory:"):
    """Load raw records, clean them with DataProcessor and store them in SQLite."""
    loader = get_loader(input_path)
    records = loader.load(input_path)
    clean = DataProcessor().process(records)
    store = SQLiteStore(db_path)
    saved = store.save(clean)
    print(f"Saved {saved} of {len(records)} records")
    return store


def report(store):
    """Print stored people and a rolling average of their ages."""
    rows = store.fetch_all()
    for name, age, city in rows:
        print(f"{name:<15} {age:>3}  {city}")
    print("rolling avg age:", rolling_average([age for _, age, _ in rows]))


def main():
    parser = argparse.ArgumentParser(description="CSV/JSON -> SQLite people pipeline")
    parser.add_argument("input_path")
    parser.add_argument("--db", default=":memory:")
    args = parser.parse_args()
    report(run_pipeline(args.input_path, args.db))


if __name__ == "__main__":
    main()
