import csv
import os


def get_bank_information_by_blz(blz):
    this_dir, this_filename = os.path.split(__file__)
    DATA_PATH = os.path.join(this_dir, "data", "banks.csv")

    with open(DATA_PATH, newline='') as data_file:
        reader = csv.DictReader(data_file)
        for row in reader:
            if row['Bankleitzahl'].strip() == blz.strip():
                return row

    return {"Bankleitzahl": blz}
