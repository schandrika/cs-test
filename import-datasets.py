#!/usr/bin/env python
"""
Import datasets into Invenio RDM from JSON files.

Reads JSON files from the ./records directory and creates/publishes records.
If a .zip file with the same base name exists, files in the zip are uploaded
to the record before publishing.

Subfolders under ./records are treated as communities. If a subfolder exists,
a community is created (or reused if it already exists) using the folder name
as the slug and a title-cased version of the name as the display title.
Records inside the subfolder are added to that community.

Directory structure example:
    records/
        dataset-1.json              <- published with no files, no community
        dataset-2.json              <- published with files, no community
        dataset-2.zip
        grid-hydro/                 <- creates/reuses community "Grid Hydro"
            dataset-3.json
            dataset-3.zip           <- files uploaded to dataset-3

If the folder contains zip files that need to be uploaded into s3, then AWS credentials must be set
in the environment before running. Use AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
AWS_SESSION_TOKEN (if using temporary SSO credentials), and AWS_DEFAULT_REGION.

Usage:
    pipenv run invenio shell import-datasets.py
"""

import json
import traceback
import zipfile
import tempfile
from pathlib import Path

from invenio_rdm_records.proxies import current_rdm_records_service, current_record_communities_service
from invenio_communities.proxies import current_communities
from invenio_access.permissions import system_identity
from invenio_db import db
from invenio_records_resources.services.uow import UnitOfWork
from invenio_search import current_search_client


def get_or_create_community(community_name):
    """
    Get an existing community by name or create it if it doesn't exist.

    Args:
        community_name: Name of the community (folder name like 'research-group' or 'research_group')

    Returns:
        Community ID
    """
    # Convert folder name to display title
    # Replace - and _ with spaces, then capitalize each word
    display_title = community_name.replace("-", " ").replace("_", " ").title()

    try:
        # Try to find existing community by slug
        communities_svc = current_communities.service
        results = communities_svc.search(
            system_identity,
            params={"q": f"slug:{community_name}"}
        )

        for hit in results:
            community_slug = hit["slug"]
            print(f"  Using existing community: {display_title} (slug: {community_slug})")
            return community_slug
    except Exception as e:
        print(f"  Could not search for community: {str(e)}")

    # Create new community if it doesn't exist
    try:
        slug = community_name.lower().replace(" ", "-").replace("_", "-")

        community_data = {
            "slug": slug,
            "metadata": {
                "title": display_title,
                "description": f"Community for {display_title} datasets"
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

        community_slug = community["slug"]
        print(f"  Created new community: {display_title} (slug: {community_slug})")
        return community_slug

    except Exception as e:
        print(f"  Failed to create community {display_title}: {str(e)}")
        return None


def sanitize_record(record_data, enable_files=False):
    """
    Convert exported Invenio JSON into a minimal valid create payload.
    This removes all system-managed / invalid fields.

    Args:
        record_data: The raw record data from JSON
        enable_files: Whether to enable files for this record (default: False)
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

    return result


def add_record_to_community(record_id, community_slug):
    """
    Add a published record directly to a community without a review request.

    Uses the add method with require_review=False which goes through the full
    community_inclusion_service.include() path, persisting the community
    membership to the parent record immediately.

    Args:
        record_id: The PID of the published record (e.g. 'n2806-b2m95')
        community_slug: The slug of the community (e.g. 'grid-hydro')
    """
    # add() is decorated with @unit_of_work() so it manages its own transaction
    processed, errors = current_record_communities_service.add(
        system_identity,
        record_id,
        data={"communities": [{"id": community_slug, "require_review": False}]}
    )

    if errors:
        print(f"  Warning: Community add errors: {errors}")
    else:
        print(f"  Added to community: {community_slug}")
        # Force index refresh so the record appears in the community immediately
        try:
            current_search_client.indices.refresh(index="*")
        except Exception as e:
            print(f"  Warning: Index refresh failed: {str(e)}")


def create_draft(record_data):
    """Create a draft record."""
    with UnitOfWork(db.session) as uow:
        draft = current_rdm_records_service.create(
            system_identity, record_data, uow=uow
        )
        uow.commit()

    return draft


def publish_draft(draft_id):
    """Publish a draft record."""
    with UnitOfWork(db.session) as uow:
        record = current_rdm_records_service.publish(
            system_identity, draft_id, uow=uow
        )
        uow.commit()

    return record


def upload_files_from_zip(draft_id, zip_path):
    """
    Extract files from a zip archive and upload them to a draft record.

    Args:
        draft_id: The ID of the draft record
        zip_path: Path to the zip file

    Returns:
        List of uploaded filenames
    """
    uploaded_files = []

    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            file_list = zip_ref.namelist()

            if not file_list:
                print(f"  Zip file is empty: {zip_path.name}")
                return uploaded_files

            print(f"  Uploading {len(file_list)} file(s) from zip...")

            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                zip_ref.extractall(temp_path)

                # Collect files to initialize
                files_to_init = []
                file_mapping = {}  # Maps cleaned filename to file path

                for file_name in file_list:
                    # Skip directories
                    if file_name.endswith('/'):
                        continue

                    file_path = temp_path / file_name

                    if not file_path.exists():
                        continue

                    clean_filename = Path(file_name).name
                    files_to_init.append({"key": clean_filename})
                    file_mapping[clean_filename] = file_path

                if not files_to_init:
                    print(f"  No files to upload in zip")
                    return uploaded_files

                # Initialize files on draft inside a UnitOfWork
                # NOTE: use draft_files (not files) — files is for published records
                try:
                    with UnitOfWork(db.session) as uow:
                        current_rdm_records_service.draft_files.init_files(
                            system_identity,
                            draft_id,
                            files_to_init,
                            uow=uow
                        )
                        uow.commit()
                except Exception as e:
                    print(f"  Could not initialize files: {str(e)}")
                    return uploaded_files

                # Upload and commit each file inside a UnitOfWork
                for clean_filename, file_path in file_mapping.items():
                    try:
                        with UnitOfWork(db.session) as uow:
                            with open(file_path, 'rb') as fp:
                                current_rdm_records_service.draft_files.set_file_content(
                                    system_identity,
                                    draft_id,
                                    clean_filename,
                                    fp,
                                    content_length=file_path.stat().st_size,
                                    uow=uow
                                )
                            current_rdm_records_service.draft_files.commit_file(
                                system_identity,
                                draft_id,
                                clean_filename,
                                uow=uow
                            )
                            uow.commit()

                        uploaded_files.append(clean_filename)
                        print(f"    Uploaded: {clean_filename}")

                    except Exception as e:
                        print(f"    Failed to upload {clean_filename}: {str(e)}")

        if uploaded_files:
            print(f"  Successfully uploaded {len(uploaded_files)} file(s)")

    except zipfile.BadZipFile:
        print(f"  Invalid zip file: {zip_path.name}")
    except Exception as e:
        print(f"  Error processing zip file: {str(e)}")
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
        community_slug = None

        # If file is in a subfolder, use folder name as community
        if len(relative_path.parts) > 1:
            community_name = relative_path.parts[0]

            # Check cache first
            if community_name not in communities_cache:
                print(f"\nProcessing community: {community_name}")
                slug = get_or_create_community(community_name)
                communities_cache[community_name] = slug

            community_slug = communities_cache[community_name]

        print(f"➡ Processing {json_file.name}")
        if community_name:
            print(f"   (in community: {community_name})")

        # Check if corresponding zip file exists
        zip_file = json_file.with_suffix('.zip')
        has_files = zip_file.exists()

        try:
            with open(json_file, "r", encoding="utf-8") as f:
                raw_data = json.load(f)

            cleaned_data = sanitize_record(
                raw_data, enable_files=has_files)

            # Step 1: Create draft
            draft = create_draft(cleaned_data)
            print(f"  ➡ Draft created (ID: {draft.id})")

            # Step 2: Upload files from zip if it exists
            uploaded_files = []
            if has_files:
                uploaded_files = upload_files_from_zip(draft.id, zip_file)
                if not uploaded_files:
                    raise Exception(
                        f"File upload failed for {zip_file.name} — not publishing draft {draft.id}"
                    )

            # Step 3: Publish the draft after files are uploaded
            record = publish_draft(draft.id)

            # Step 4: Add to community after publishing (bypasses review)
            if community_slug:
                try:
                    add_record_to_community(record.id, community_slug)
                except Exception as e:
                    print(f"  Warning: Could not add to community: {str(e)}")

            results.append({
                "source_file": json_file.name,
                "record_id": record.id,
                "title": record["metadata"].get("title", "N/A"),
                "files_uploaded": len(uploaded_files),
                "community": community_name
            })
            print(f"Success: {json_file.name} -> Record ID: {record.id}")
            if community_slug:
                print(f"  Community: {community_slug}")
            print()

        except Exception as e:
            print(f"Failed: {json_file.name}")
            print(f"Error: {str(e)}\n")
            traceback.print_exc()
            failed.append(json_file.name)

    # Print summary
    print(f"\n{'='*60}")
    print(f"Import Summary:")
    print(f"  Successfully imported: {len(results)}")
    print(f"  Failed imports: {len(failed)}")
    if communities_cache:
        print(f"  Communities created/used: {len(communities_cache)}")
    print(f"{'='*60}")

    if results:
        print("\nSuccessfully imported records:")
        for result in results:
            files_info = f" ({result['files_uploaded']} files)" if result['files_uploaded'] > 0 else ""
            community_info = f" [Community: {result['community']}]" if result['community'] else ""
            print(
                f"  - {result['title']} (ID: {result['record_id']}){files_info}{community_info}")

    if failed:
        print("\nFailed to import:")
        for filename in failed:
            print(f"  - {filename}")

    return results, failed


if __name__ == "__main__":
    ingest_all_records()
