from pymongo import MongoClient
import os

def verify_bin_data():
    mongo_uri = "mongodb+srv://jokerdbUser:Ak1f1987%21...@paymentmanger.gvaavzc.mongodb.net/mydb?retryWrites=true&w=majority"

    client = MongoClient(mongo_uri)

    db = client["mydb"]
    collection = db["binList"]

    cursor = collection.find().limit(100)

    print("=== SAMPLE DATA ===")

    for i, doc in enumerate(cursor, 1):
        print(i, doc.get("BIN"), doc.get("CountryName"))

    print("DONE")

if __name__ == "__main__":
    verify_bin_data()