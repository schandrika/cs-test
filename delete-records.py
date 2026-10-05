#!/usr/bin/env python
"""
Delete published records from Invenio RDM.

Usage:
    # Delete specific records from a file (one ID per line):
    pipenv run invenio shell delete_records.py record_ids.txt

    # DEV ONLY - delete ALL records (omit the file argument):
    pipenv run invenio shell delete_records.py

WARNING: Deletion is a soft-delete (tombstone). Records are marked deleted
         and removed from search, but data remains in the database.
         This is intentional Invenio behaviour.

# # ───────────────────────────delete all option ─────────────────────────────────
# # The no-argument "delete all" mode is intended for dev/test cleanup only.
# # uncomment the else part in main to enable delete all
# # ─────────────────────────────────────────────────────────────────────────────
"""

import sys

from invenio_rdm_records.proxies import current_rdm_records_service
from invenio_access.permissions import system_identity
from invenio_db import db
from invenio_records_resources.services.uow import UnitOfWork
from invenio_search import current_search_client

# Tombstone data required by delete_record()
TOMBSTONE = {
    "removal_reason": {"id": "spam"},
    "note": "Deleted by migration cleanup script"
}


def delete_record(record_id):
    """
    Soft-delete a single published record.

    Args:
        record_id: The PID of the published record (e.g. 'n2806-b2m95')

    Returns:
        True if successful, False otherwise
    """
    try:
        with UnitOfWork(db.session) as uow:
            current_rdm_records_service.delete_record(
                system_identity,
                record_id,
                data=TOMBSTONE,
                uow=uow
            )
            uow.commit()
        print(f"  Deleted: {record_id}")
        return True
    except Exception as e:
        print(f"  Failed to delete {record_id}: {str(e)}")
        return False


def get_all_record_ids():
    """
    Scan and return all published record IDs.
    DEV ONLY - used when no IDs file is provided.
    """
    print("Scanning for all published records...")
    records = current_rdm_records_service.scan(system_identity)
    ids = [hit["id"] for hit in records]
    print(f"Found {len(ids)} record(s).\n")
    return ids


def delete_records(record_ids):
    """Delete a list of records and refresh the search index."""
    print(f"Deleting {len(record_ids)} record(s)...\n")

    success = []
    failed = []

    for record_id in record_ids:
        record_id = record_id.strip()
        if not record_id:
            continue

        print(f"Processing {record_id}...")
        if delete_record(record_id):
            success.append(record_id)
        else:
            failed.append(record_id)

    # Refresh index after deletions
    if success:
        try:
            current_search_client.indices.refresh(index="*")
            print("\nSearch index refreshed.")
        except Exception as e:
            print(f"\nWarning: Index refresh failed: {str(e)}")

    # Summary
    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Successfully deleted: {len(success)}")
    print(f"  Failed:               {len(failed)}")
    print(f"{'='*60}")

    if success:
        print("\nDeleted records:")
        for rid in success:
            print(f"  - {rid}")

    if failed:
        print("\nFailed to delete:")
        for rid in failed:
            print(f"  - {rid}")


def main():
    if len(sys.argv) >= 2:
        # Specific records from file
        ids_file = sys.argv[1]
        try:
            with open(ids_file, "r") as f:
                record_ids = [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            print(f"Error: File '{ids_file}' not found.")
            sys.exit(1)

        if not record_ids:
            print(f"Error: No record IDs found in '{ids_file}'.")
            sys.exit(1)
    else:
        print(f"Missing argument.No input file with list of ids to delete")
        sys.exit(1)
    # else:
    #     # ── DEV ONLY: delete all records option ─────────────────────────────────────
    #     print("WARNING: No IDs file provided. Deleting ALL records (dev mode).")
    #     confirm = input("Type 'yes' to confirm: ").strip().lower()
    #     if confirm != "yes":
    #         print("Aborted.")
    #         sys.exit(0)
    #     record_ids = get_all_record_ids()
    #     if not record_ids:
    #         print("No records found.")
    #         sys.exit(0)
    #     # ─────────────────────────────────────────────────────────────────────

    delete_records(record_ids)


if __name__ == "__main__":
    main()
