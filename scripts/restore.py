"""Restore an encrypted backup only into an EMPTY, separately named database.

No production overwrite option exists. Restore attachments into a separate local
directory for verification before publishing to a fresh private object bucket.
"""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile
from cryptography.fernet import Fernet
import psycopg
from backup import postgres_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--files-output", type=Path, required=True)
    args = parser.parse_args()
    url = os.environ["RESTORE_DATABASE_URL"]
    if not args.files_output.exists():
        args.files_output.mkdir(parents=True)
    if any(args.files_output.iterdir()):
        parser.error("Attachment restore directory must be empty.")
    with psycopg.connect(url) as conn:
        if conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
        ).fetchone()[0]:
            parser.error("Restore target must be an empty database.")
    content = Fernet(os.environ["BACKUP_ENCRYPTION_KEY"].encode()).decrypt(args.input.read_bytes())
    with zipfile.ZipFile(io.BytesIO(content)) as archive, tempfile.TemporaryDirectory() as temporary:
        manifest = json.loads(archive.read("manifest.json"))
        dump = archive.read("database.dump")
        if hashlib.sha256(dump).hexdigest() != manifest["database_sha256"]:
            raise ValueError("Database checksum failed.")
        root = args.files_output.resolve()
        for item in manifest["files"]:
            target = (root / item["key"]).resolve()
            if not target.is_relative_to(root):
                raise ValueError("Invalid attachment path.")
            data = archive.read("files/" + item["key"])
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("Attachment checksum failed.")
        dump_path = Path(temporary) / "database.dump"
        dump_path.write_bytes(dump)
        subprocess.run(
            [
                os.getenv("PG_RESTORE", "pg_restore"),
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
                "--single-transaction",
                "--dbname=" + postgres_environment(url)["PGDATABASE"],
                str(dump_path),
            ],
            env=postgres_environment(url),
            check=True,
            capture_output=True,
        )
        for item in manifest["files"]:
            target = root / item["key"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read("files/" + item["key"]))
    print(
        "Restore completed. Apply runtime grants and verify isolation, row counts, and file access before use."
    )


if __name__ == "__main__":
    main()
