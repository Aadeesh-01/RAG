class BaseProcessor:
    """Template for record processing: validate every record, then transform it."""

    required_fields = ()

    def process(self, records):
        valid = [r for r in records if self.validate(r)]
        return [self.transform(r) for r in valid]

    def validate(self, record):
        return all(record.get(field) not in (None, "") for field in self.required_fields)

    def transform(self, record):
        return record


class DataProcessor(BaseProcessor):
    """Cleans people records: strips whitespace, normalises names and casts ages."""

    required_fields = ("name", "age")

    def transform(self, record):
        cleaned = {key.strip().lower(): str(value).strip() for key, value in record.items()}
        cleaned["name"] = normalize_name(cleaned["name"])
        cleaned["age"] = int(cleaned["age"])
        return cleaned


def normalize_name(name):
    """Collapse internal whitespace and title-case a person's name."""
    return " ".join(part.capitalize() for part in name.split())


def rolling_average(values, window=3):
    """Simple moving average used for reporting age trends."""
    if window <= 0:
        raise ValueError("window must be positive")
    averages = []
    for i in range(len(values) - window + 1):
        averages.append(sum(values[i:i + window]) / window)
    return averages
