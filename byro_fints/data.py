import csv
import os


class excel_semicolon(csv.excel):
    """Describe the usual properties of Excel semicolon delimited files (Germany)."""
    delimiter = ';'


def get_bank_information_by_blz(blz):
    this_dir, this_filename = os.path.split(__file__)
    DATA_PATH = os.path.join(this_dir, "data", "banks.csv")

    with open(DATA_PATH, newline='', encoding='cp1252') as data_file:
        reader = csv.DictReader(data_file, dialect=excel_semicolon)
        for row in reader:
            if row['BLZ'].strip() == blz.strip():
                return row

    return {"BLZ": blz}
