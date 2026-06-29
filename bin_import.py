import csv
import certifi
from pymongo import MongoClient

MONGO_URI = "mongodb+srv://cardmarketApp:gnbqHdTrlceMZjOS@paymentmanger.gvaavzc.mongodb.net/mydb?retryWrites=true&w=majority"


def import_bin_data():
    client = MongoClient(
        MONGO_URI,
        tls=True,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=30000
    )

    db = client["mydb"]
    collection = db["binList"]

    inserted = 0
    updated = 0
    skipped = 0

    with open("bin-data.csv", "r", encoding="utf-8") as file:
        reader = csv.DictReader(file, delimiter=",")

        print("FIELDS:", reader.fieldnames)

        for row in reader:
            bin_value = (row.get("BIN") or "").strip()

            if not bin_value:
                skipped += 1
                continue

            doc = {
                "BIN": bin_value,
                "Brand": row.get("Brand", "").strip(),
                "Type": row.get("Type", "").strip(),
                "Category": row.get("Category", "").strip(),
                "Issuer": row.get("Issuer", "").strip(),
                "IssuerPhone": row.get("IssuerPhone", "").strip(),
                "IssuerUrl": row.get("IssuerUrl", "").strip(),
                "isoCode2": row.get("isoCode2", "").strip(),
                "isoCode3": row.get("isoCode3", "").strip(),
                "CountryName": row.get("CountryName", "").strip(),
            }

            result = collection.update_one(
                {"BIN": bin_value},
                {"$set": doc},
                upsert=True
            )

            if result.upserted_id:
                inserted += 1
            else:
                updated += 1

            print("✔", bin_value)

    print("\nDONE")
    print("Inserted:", inserted)
    print("Updated:", updated)
    print("Skipped:", skipped)


if __name__ == "__main__":
    import_bin_data()