import sys
import os
import msal
import time
from pathlib import Path
from office365.graph_client import GraphClient
from office365.runtime.odata.v4.upload_session_request import UploadSessionRequest
from office365.onedrive.driveitems.driveItem import DriveItem
from office365.onedrive.internal.paths.url import UrlPath
from office365.runtime.queries.upload_session import UploadSessionQuery
from office365.onedrive.driveitems.uploadable_properties import DriveItemUploadableProperties
from office365.runtime.client_request_exception import ClientRequestException

site_name = sys.argv[1]
sharepoint_host_name = sys.argv[2]
tenant_id = sys.argv[3]
client_id = sys.argv[4]
client_secret = sys.argv[5]
upload_path = sys.argv[6]
file_path = sys.argv[7]
max_retry = int(sys.argv[8]) or 3
login_endpoint = sys.argv[9] or "login.microsoftonline.com"
graph_endpoint = sys.argv[10] or "graph.microsoft.com"
delete_before_upload = True if len(sys.argv) == 12 and sys.argv[11] == "true" else False

# below used with 'get_by_url' in GraphClient calls
tenant_url = f'https://{sharepoint_host_name}/sites/{site_name}'

local_files = Path.cwd().glob(pattern=file_path)

def acquire_token() -> dict:
    """
    Acquire token via MSAL
    """
    authority_url = f'https://{login_endpoint}/{tenant_id}'
    app = msal.ConfidentialClientApplication(
        authority=authority_url,
        client_id=client_id,
        client_credential=client_secret
    )
    token = app.acquire_token_for_client(scopes=[f"https://{graph_endpoint}/.default"])

    if not token:
        raise ValueError("Failed to acquire token")

    return token

client = GraphClient(acquire_token)

def progress_status(offset, file_size):
    print(f"Uploaded {offset} bytes from {file_size} bytes ... {offset/file_size*100:.2f}%")

def success_callback(remote_file):
    print(f"File {remote_file.web_url} has been uploaded")

def resumable_upload(drive, local_path, file_size, chunk_size, max_chunk_retry, timeout_secs):
    def _start_upload():
        with open(local_path, "rb") as local_file:
            session_request = UploadSessionRequest(
                local_file, 
                chunk_size, 
                lambda offset: progress_status(offset, file_size)
            )
            retry_seconds = timeout_secs / max_chunk_retry
            for session_request._range_data in session_request._read_next():
                for retry_number in range(max_chunk_retry):
                    try:
                        super(UploadSessionRequest, session_request).execute_query(qry)
                        break
                    except Exception as e:
                        if retry_number + 1 >= max_chunk_retry:
                            raise e
                        print(f"Retry {retry_number}: {e}")
                        time.sleep(retry_seconds)
    
    file_name = os.path.basename(local_path)
    return_type = DriveItem(
        drive.context, 
        UrlPath(file_name, drive.resource_path))
    qry = UploadSessionQuery(
        return_type, {"item": DriveItemUploadableProperties(name=file_name)})
    drive.context.add_query(qry).after_query_execute(_start_upload)
    return_type.get().execute_query()
    success_callback(return_type)

def delete_file(local_path: Path):
    local_path_sharepoint = local_path.relative_to(Path.cwd())
    drive_file = client.sites.get_by_url(tenant_url).drive.root.get_by_path(str(upload_path / local_path_sharepoint))

    try:
        file = drive_file.get().execute_query()
    except ClientRequestException as e:
        print(f"File {local_path_sharepoint} not read from Sharepoint, skipping deleting it... Error code: {e.code}")
        return

    if file.id:
        file.delete_object().execute_query()
        print(f"File {local_path_sharepoint} has been deleted from {tenant_url}/{upload_path}")
    else:
        print(f"File {local_path_sharepoint} seems not to exist in {tenant_url}/{upload_path}")
        return

def upload_file(local_path: Path, chunk_size: int):
    local_path_sharepoint = local_path.relative_to(Path.cwd())

    drive_folder = client.sites.get_by_url(tenant_url).drive.root.get_by_path(str(upload_path / local_path_sharepoint.parent))

    file_size = os.path.getsize(local_path)
    if file_size < chunk_size:
        remote_file = drive_folder.upload_file(str(local_path_sharepoint)).execute_query()
        success_callback(remote_file)
    else:
        resumable_upload(
            drive_folder, 
            local_path, 
            file_size, 
            chunk_size, 
            max_chunk_retry=60, 
            timeout_secs=10*60
        )

print (f"Uploading files to {tenant_url}/{upload_path}")
if delete_before_upload:
    print(f"--> Deleting files from {tenant_url}/{upload_path} before uploading...")
for f in local_files:
    if f.is_dir():
        continue
    for i in range(max_retry):
        try:
            if delete_before_upload:
                delete_file(f)
            upload_file(f, 2*1024*1024)
            break
        except Exception as e:
            print(f"Unexpected error occurred: {e}, {type(e)}")
            if i == max_retry - 1:
                raise e
