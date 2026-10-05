#!/usr/bin/env python
"""
Add existing published records to a community.

Usage:
    pipenv run invenio shell add_records_to_community.py <community_slug> <ids_file>

Arguments:
    community_slug  The slug of the target community (e.g. 'grid-hydro')
    ids_file        Path to a file with one record ID per line

Example:
    pipenv run invenio shell add_records_to_community.py grid-hydro record_ids.txt
"""

import os
import sys

from invenio_rdm_records.proxies import current_record_communities_service
from invenio_access.permissions import system_identity
from invenio_search import current_search_client


def add_record_to_community(record_id, community_slug):
    """
    Add a single published record to a community without a review request.

    Args:
        record_id: The PID of the published record (e.g. 'n2806-b2m95')
        community_slug: The slug of the community (e.g. 'grid-hydro')

    Returns:
        True if successful, False otherwise
    """
    try:
        processed, errors = current_record_communities_service.add(
            system_identity,
            record_id,
            data={"communities": [{"id": community_slug, "require_review": False}]}
        )

        if errors:
            print(f"  Warning: {errors}")
            return False
        else:
            print(f"  Added {record_id} to community: {community_slug}")
            return True

    except Exception as e:
        print(f"  Failed to add {record_id}: {str(e)}")
        return False


def add_records_to_community(record_ids, community_slug):
    """
    Add multiple records to a community and refresh the index.

    Args:
        record_ids: List of record PIDs
        community_slug: The slug of the community
    """
    print(f"Adding {len(record_ids)} record(s) to community: {community_slug}\n")

    success = []
    failed = []

    for record_id in record_ids:
        record_id = record_id.strip()
        if not record_id:
            continue

        print(f"Processing {record_id}...")
        if add_record_to_community(record_id, community_slug):
            success.append(record_id)
        else:
            failed.append(record_id)

    # Refresh index so records appear in community immediately
    if success:
        try:
            current_search_client.indices.refresh(index="*")
            print("\nSearch index refreshed.")
        except Exception as e:
            print(f"\nWarning: Index refresh failed: {str(e)}")

    # Summary
    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Successfully added: {len(success)}")
    print(f"  Failed:             {len(failed)}")
    print(f"{'='*60}")

    if success:
        print("\nSuccessfully added:")
        for rid in success:
            print(f"  - {rid}")

    if failed:
        print("\nFailed:")
        for rid in failed:
            print(f"  - {rid}")


def main():
    # sys.argv[0] is the script name, [1] is community_slug, [2] is ids_file
    if len(sys.argv) < 3:
        print("Usage: pipenv run invenio shell add_records_to_community.py <community_slug> <ids_file>")
        print("Example: pipenv run invenio shell add_records_to_community.py grid-hydro record_ids.txt")
        sys.exit(1)

    community_slug = sys.argv[1]
    ids_file = sys.argv[2]

    # Resolve relative paths from the script's directory
    if not os.path.isabs(ids_file):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ids_file = os.path.join(script_dir, ids_file)

    try:
        with open(ids_file, "r") as f:
            record_ids = [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        print(f"Error: File '{ids_file}' not found.")
        sys.exit(1)

    if not record_ids:
        print(f"Error: No record IDs found in '{ids_file}'.")
        sys.exit(1)

    add_records_to_community(record_ids, community_slug)


if __name__ == "__main__":
    main()
