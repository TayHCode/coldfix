from datetime import date, timedelta


def parse_date(text):
    year, month, day = (int(part) for part in text.split("-"))
    return date(year, month, 1) + timedelta(days=day - 1)
