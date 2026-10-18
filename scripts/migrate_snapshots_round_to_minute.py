"""
Normaliza los snapshots en funding_arbitrage_pairs_ts al inicio del minuto UTC.
Uso:
    python scripts/migrate_snapshots_round_to_minute.py --batch 2000 --max-batches 200
Política: al upsert prevalece el último procesado (suele ser el más reciente).
"""
import argparse
from datetime import datetime, timezone
import pymongo


def minute_floor(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.replace(second=0, microsecond=0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="mongodb://root:example@mongo:27017")
    parser.add_argument("--db", default="tfh")
    parser.add_argument("--batch", type=int, default=2000)
    parser.add_argument("--max-batches", type=int, default=200)
    args = parser.parse_args()

    client = pymongo.MongoClient(args.uri)
    col = client[args.db]["funding_arbitrage_pairs_ts"]

    migrated = 0
    deleted = 0
    batches = 0

    while batches < args.max_batches:
        batches += 1
        cursor = col.find(
            {
                "$or": [
                    {"$expr": {"$ne": [{"$second": "$timestamp"}, 0]}},
                    {"$expr": {"$ne": [{"$millisecond": "$timestamp"}, 0]}},
                ]
            }
        ).limit(args.batch)
        docs = list(cursor)
        if not docs:
            break
        ops = []
        ids = []
        for doc in docs:
            doc_id = doc.pop("_id", None)
            ts = doc.get("timestamp")
            if not ts:
                ids.append(doc_id)
                continue
            ts_norm = minute_floor(ts)
            doc["timestamp"] = ts_norm
            doc.setdefault("source", "live")
            ops.append(
                pymongo.UpdateOne(
                    {"pair_id": doc.get("pair_id"), "timestamp": ts_norm, "source": doc.get("source")},
                    {"$set": doc},
                    upsert=True,
                )
            )
            ids.append(doc_id)
        if ops:
            res = col.bulk_write(ops, ordered=False)
            migrated += res.upserted_count + res.modified_count
        if ids:
            del_res = col.delete_many({"_id": {"$in": ids}})
            deleted += del_res.deleted_count

    print(
        {
            "migrated": migrated,
            "deleted": deleted,
            "batches": batches,
        }
    )


if __name__ == "__main__":
    main()
