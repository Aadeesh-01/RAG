import csv
import json


class BaseLoader:
    """Common interface for everything that reads raw records from disk."""

    def load(self, path):
        raise NotImplementedError

    def describe(self):
        return f"{type(self).__name__} reading raw records"


class CSVLoader(BaseLoader):
    """Reads a CSV file into a list of dicts, one per row."""

    def __init__(self, delimiter=","):
        self.delimiter = delimiter

    def load(self, path):
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f, delimiter=self.delimiter))


class JSONLoader(BaseLoader):
    """Reads a JSON array of objects."""

    def load(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)


def get_loader(path):
    """Pick a loader based on the file extension."""
    if path.endswith(".json"):
        return JSONLoader()
    return CSVLoader()
