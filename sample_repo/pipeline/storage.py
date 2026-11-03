import sqlite3


class SQLiteStore:
    """Persists processed records in a SQLite table."""

    def __init__(self, db_path=":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.create_table()

    def create_table(self):
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS people (name TEXT, age INTEGER, city TEXT)"
        )

    def save(self, records):
        rows = [(r["name"], r["age"], r.get("city", "")) for r in records]
        self.conn.executemany("INSERT INTO people VALUES (?, ?, ?)", rows)
        self.conn.commit()
        return len(rows)

    def fetch_all(self):
        return self.conn.execute("SELECT name, age, city FROM people").fetchall()
