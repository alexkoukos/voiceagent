"""Copy every recording from one S3-compatible bucket to another (EU move).

A Railway bucket's region is fixed when it is created, so moving recordings to the EU means
a new bucket in `ams` and a copy. Objects are copied byte for byte: sealed (encrypted)
recordings stay sealed, and calls.recording_url stores only the key, so nothing in the
database changes.

    SRC_ENDPOINT=... SRC_KEY_ID=... SRC_SECRET=... SRC_BUCKET=... \
    DST_ENDPOINT=... DST_KEY_ID=... DST_SECRET=... DST_BUCKET=... \
    uv run --python 3.12 --with-requirements requirements.txt python scripts/copy_bucket.py [--apply]

Without --apply it only lists what it would copy. Existing objects of the same size in the
destination are skipped, so the script can be rerun after new calls during the switch.
"""

import os
import sys

import boto3


def client(prefix: str):
    return boto3.client(
        "s3", endpoint_url=os.environ[f"{prefix}_ENDPOINT"], region_name=os.environ.get(f"{prefix}_REGION", "auto"),
        aws_access_key_id=os.environ[f"{prefix}_KEY_ID"], aws_secret_access_key=os.environ[f"{prefix}_SECRET"],
    )


def objects(s3, bucket: str) -> dict[str, int]:
    found = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            found[item["Key"]] = item["Size"]
    return found


def main(apply: bool) -> None:
    src, dst = client("SRC"), client("DST")
    src_bucket, dst_bucket = os.environ["SRC_BUCKET"], os.environ["DST_BUCKET"]
    have = objects(dst, dst_bucket)
    todo = {k: size for k, size in objects(src, src_bucket).items() if have.get(k) != size}
    print(f"{len(todo)} objects to copy ({sum(todo.values()) / 1e6:.1f} MB), {len(have)} already in destination")
    if not apply:
        return
    for i, key in enumerate(sorted(todo), 1):
        body = src.get_object(Bucket=src_bucket, Key=key)["Body"].read()
        dst.put_object(Bucket=dst_bucket, Key=key, Body=body)
        if dst.head_object(Bucket=dst_bucket, Key=key)["ContentLength"] != todo[key]:
            sys.exit(f"size mismatch after copying {key}")
        print(f"{i}/{len(todo)} {key}")
    print("done; keep the source bucket until the app has played recordings from the new one")


if __name__ == "__main__":
    main("--apply" in sys.argv[1:])
