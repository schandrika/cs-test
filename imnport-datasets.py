#!/usr/bin/env python
"""
Import datasets into Invenio RDM from JSON files.
Reads JSON files from ./records directory and creates/publishes records.
If a .zip file with the same base name exists, uploads files from the zip.

Usage:
    python import_datasets.py
"""

import json
import traceback
import zipfile
import tempfile
from pathlib import Path

from invenio_rdm_records.proxies import current_rdm_records_service
from invenio_communities.proxies import current_communities
from invenio_access.permissions import system_identity
from invenio_db import db
from invenio_records_resources.services.uow import UnitOfWork


def get_or_create_community(community_name):
    """
    Get an existing community by name or create it if it doesn't exist.

    Args:
        community_name: Name of the community (also used as slug)

    Returns:
        Community ID
    """
    try:
        # Try to find existing community by slug
        communities_svc = current_communities.service
        results = communities_svc.search(
            system_identity,
            params={"q": f"slug:{community_name}"}
        )

        if len(results) > 0:
            community_id = results[0]["id"]
            print(f"  ✓ Using existing community: {community_name} (ID: {community_id})")
            return community_id
    except Exception as e:
        print(f"  ⚠ Could not search for community: {str(e)}")

    # Create new community if it doesn't exist
    try:
        slug = community_name.lower().replace(" ", "-").replace("_", "-")

        community_data = {
            "metadata": {
                "title": community_name,
                "description": f"Community for {community_name} datasets"
            },
            "access": {
                "visibility": "public"
            }
        }

        with UnitOfWork(db.session) as uow:
            community = current_communities.service.create(
                system_identity, community_data, uow=uow
            )
            uow.commit()

        community_id = community.id
        print(f"  ✓ Created new community: {community_name} (ID: {community_id})")
        return community_id

    except Exception as e:
        print(f"  ✗ Failed to create community {community_name}: {str(e)}")
        return None


def sanitize_record(record_data, enable_files=False, community_id=None):
    """
    Convert exported Invenio JSON into a minimal valid create payload.
    This removes all system-managed / invalid fields.

    Args:
        record_data: The raw record data from JSON
        enable_files: Whether to enable files for this record (default: False)
        community_id: Optional community ID to add record to
    """

    metadata = record_data.get("metadata", {})

    result = {
        "metadata": {
            "title": metadata.get("title", "Untitled"),
            "publication_date": (
                metadata.get("publication_date", "2016").split("/")[0]
                if metadata.get("publication_date")
                else "2016"
            ),
            "resource_type": metadata.get("resource_type", {"id": "dataset"}),
            "creators": metadata.get(
                "creators",
                [
                    {
                        "person_or_org": {
                            "type": "organizational",
                            "name": "Unknown"
                        }
                    }
                ],
            ),
            "description": metadata.get("description", ""),
            "contributors": metadata.get("contributors", []),
            "subjects": metadata.get("subjects", []),
            "keywords": metadata.get("keywords", []),
        },
        "files": {"enabled": enable_files},
        "access": {
            "record": "public",
            "files": "public"
        }
    }

    # Add community if provided
    if community_id:
        result["communities"] = [{"id": community_id}]

    return result


def create_and_publish(record_data, enable_files=False):
    """Create + publish a single record inside a unit of work."""
    with UnitOfWork(db.session) as uow:
        draft = current_rdm_records_service.create(
            system_identity, record_data, uow=uow
        )

        record = current_rdm_records_service.publish(
            system_identity, draft.id, uow=uow
        )

        uow.commit()

    return record


def upload_files_from_zip(record_id, zip_path):
    """
    Extract files from a zip archive and upload them to a record.

    Args:
        record_id: The ID of the published record
        zip_path: Path to the zip file

    Returns:
        List of uploaded filenames
    """
    uploaded_files = []

    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            file_list = zip_ref.namelist()

            if not file_list:
                print(f"  ⚠ Zip file is empty: {zip_path.name}")
                return uploaded_files

            print(f"  ➡ Uploading {len(file_list)} file(s) from zip...")

            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                zip_ref.extractall(temp_path)

                for file_name in file_list:
                    # Skip directories
                    if file_name.endswith('/'):
                        continue

                    file_path = temp_path / file_name

                    if not file_path.exists():
                        continue

                    try:
                        with open(file_path, 'rb') as f:
                            # Use the files service to add file to record
                            current_rdm_records_service.files.init_files(
                                system_identity,
                                record_id,
                                files=[{
                                    "key": Path(file_name).name,  # Use just the filename
                                }]
                            )

                            # Upload the file content
                            current_rdm_records_service.files.upload_file(
                                system_identity,
                                record_id,
                                Path(file_name).name,
                                f
                            )

                            uploaded_files.append(Path(file_name).name)
                            print(f"    ✔ Uploaded: {Path(file_name).name}")

                    except Exception as e:
                        print(f"    ✖ Failed to upload {file_name}: {str(e)}")

        if uploaded_files:
            print(f"  ✔ Successfully uploaded {len(uploaded_files)} file(s)")

    except zipfile.BadZipFile:
        print(f"  ✖ Invalid zip file: {zip_path.name}")
    except Exception as e:
        print(f"  ✖ Error processing zip file: {str(e)}")
        traceback.print_exc()

    return uploaded_files


def ingest_all_records():
    """Main entry point: ingest all JSON files in ./records, with support for community subfolders"""

    script_dir = Path(__file__).parent
    records_dir = script_dir / "records"

    if not records_dir.exists():
        raise ValueError(f"Missing records directory: {records_dir}")

    # Find all JSON files recursively
    json_files = sorted(records_dir.rglob("*.json"))

    if not json_files:
        print("No JSON files found.")
        return []

    results = []
    failed = []
    communities_cache = {}  # Cache for community IDs

    print(f"Found {len(json_files)} file(s) to import\n")

    for json_file in json_files:
        # Determine if this file is in a subfolder (community)
        relative_path = json_file.relative_to(records_dir)
        community_name = None
        community_id = None

        # If file is in a subfolder, use folder name as community
        if len(relative_path.parts) > 1:
            community_name = relative_path.parts[0]

            # Check cache first
            if community_name not in communities_cache:
                print(f"\n📁 Processing community: {community_name}")
                comm_id = get_or_create_community(community_name)
                communities_cache[community_name] = comm_id

            community_id = communities_cache[community_name]

        print(f"➡ Processing {json_file.name}")
        if community_name:
            print(f"   (in community: {community_name})")

        # Check if corresponding zip file exists
        zip_file = json_file.with_suffix('.zip')
        has_files = zip_file.exists()

        try:
            with open(json_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            cleaned_data = sanitize_record(raw_data, enable_files=has_files, community_id=community_id)

            record = create_and_publish(cleaned_data, enable_files=has_files)

            # Upload files from zip if it exists
            uploaded_files = []
            if has_files:
                uploaded_files = upload_files_from_zip(record.id, zip_file)

            results.append({
                "source_file": json_file.name,
                "record_id": record.id,
                "title": record["metadata"].get("title", "N/A"),
                "files_uploaded": len(uploaded_files),
                "community": community_name
            })
            print(f"✔ Success: {json_file.name} -> Record ID: {record.id}\n")

        except Exception as e:
            print(f"✖ Failed: {json_file.name}")
            print(f"  Error: {str(e)}\n")
            traceback.print_exc()
            failed.append(json_file.name)

    # Print summary
    print(f"\n{'='*60}")
    print(f"Import Summary:")
    print(f"  ✔ Successfully imported: {len(results)}")
    print(f"  ✖ Failed imports: {len(failed)}")
    if communities_cache:
        print(f"  📁 Communities created/used: {len(communities_cache)}")
    print(f"{'='*60}")

    if results:
        print("\nSuccessfully imported records:")
        for result in results:
            files_info = f" ({result['files_uploaded']} files)" if result['files_uploaded'] > 0 else ""
            community_info = f" [Community: {result['community']}]" if result['community'] else ""
            print(f"  - {result['title']} (ID: {result['record_id']}){files_info}{community_info}")

    if failed:
        print("\nFailed to import:")
        for filename in failed:
            print(f"  - {filename}")

    return results, failed


if __name__ == "__main__":
    ingest_all_records()
