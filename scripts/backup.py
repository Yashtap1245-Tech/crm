"""Encrypted, consistent PostgreSQL + immutable-attachment backup.

Run on a trusted operational runner, with BACKUP_DATABASE_URL, not the web role.
The destination must be independently copied off-host and monitored.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import unquote, urlparse, parse_qs
import zipfile

from cryptography.fernet import Fernet
import psycopg


def postgres_environment(url):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    env = os.environ.copy()
    env.update(
        PGHOST=parsed.hostname or "",
        PGPORT=str(parsed.port or 5432),
        PGUSER=unquote(parsed.username or ""),
        PGPASSWORD=unquote(parsed.password or ""),
        PGDATABASE=unquote(parsed.path.lstrip("/")),
    )
    if "sslmode" in query:
        env["PGSSLMODE"] = query["sslmode"][0]
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new backup filename; existing backups are never overwritten.")
    url = os.environ["BACKUP_DATABASE_URL"]
    cipher = Fernet(os.environ["BACKUP_ENCRYPTION_KEY"].encode())
    pg_env = postgres_environment(url)
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        dump = directory / "database.dump"
        bundle = directory / "backup.zip"
        with psycopg.connect(url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            rows = conn.execute("SELECT key,sha256 FROM crmapp_attachment").fetchall()
            subprocess.run(
                [
                    os.getenv("PG_DUMP", "pg_dump"),
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--snapshot=" + snapshot,
                    "--file=" + str(dump),
                ],
                env=pg_env,
                check=True,
                capture_output=True,
            )
            manifest = {"database_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(), "files": []}
            with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(dump, "database.dump")
                for key, digest in rows:
                    if os.getenv("S3_BUCKET"):
                        import boto3

                        client = boto3.client(
                            "s3",
                            endpoint_url=os.getenv("S3_ENDPOINT_URL"),
                            region_name=os.getenv("S3_REGION", "auto"),
                        )
                        data = client.get_object(Bucket=os.environ["S3_BUCKET"], Key=key)["Body"].read()
                    else:
                        data = (Path(os.environ["PRIVATE_MEDIA_ROOT"]) / key).read_bytes()
                    if hashlib.sha256(data).hexdigest() != digest:
                        raise ValueError("Attachment integrity failed; backup aborted.")
                    archive.writestr("files/" + key, data)
                    manifest["files"].append({"key": key, "sha256": digest})
                archive.writestr("manifest.json", json.dumps(manifest))
        encrypted = cipher.encrypt(bundle.read_bytes())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as output:
            output.write(encrypted)
    print(f"Encrypted database and {len(rows)} attachment(s) backed up successfully.")


if __name__ == "__main__":
    main()
