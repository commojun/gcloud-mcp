#!/usr/bin/env python3
"""
MCP Server for Google Workspace (Sheets + Drive) using gcloud OAuth credentials.

Uses `gcloud auth print-access-token` to obtain an access token and calls
Google Sheets API v4 / Drive API v3. The gcloud CLI must already be authenticated
with Drive scope:  gcloud auth login --enable-gdrive-access
"""

import asyncio
import json
import os
import re
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_API = "https://www.googleapis.com/drive/v3"
GCLOUD_PATH = os.environ.get("GCLOUD_PATH", "gcloud")

# Google Workspace MIME types → text export format
GOOGLE_MIME_EXPORT: dict[str, str] = {
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.presentation": "text/plain",
}

mcp = FastMCP("gcloud_workspace_mcp")

# ---------------------------------------------------------------------------
# Token acquisition
# ---------------------------------------------------------------------------

async def get_access_token() -> tuple[Optional[str], Optional[str]]:
    """Return (token, None) on success or (None, error_message) on failure."""
    try:
        process = await asyncio.create_subprocess_exec(
            GCLOUD_PATH,
            "auth",
            "print-access-token",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            err = stderr.decode().strip()
            return None, (
                f"Error: gcloud auth failed: {err}\n\n"
                "To authenticate, run:\n"
                "  gcloud auth login --enable-gdrive-access"
            )
        return stdout.decode().strip(), None
    except FileNotFoundError:
        return None, (
            f"Error: gcloud CLI not found at '{GCLOUD_PATH}'.\n"
            "Install from https://cloud.google.com/sdk/docs/install\n"
            "Or set the GCLOUD_PATH environment variable to the full path."
        )


# ---------------------------------------------------------------------------
# URL / ID extraction helpers
# ---------------------------------------------------------------------------

def extract_spreadsheet_id(url_or_id: str) -> str:
    """Extract spreadsheet ID from a Sheets URL, or return the input unchanged."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_id)
    return m.group(1) if m else url_or_id


def extract_drive_file_id(url_or_id: str) -> str:
    """Extract file/folder ID from a Drive or Docs URL, or return the input unchanged."""
    # Drive file: .../file/d/{ID}/...
    m = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    # Drive folder: .../drive/folders/{ID}
    m = re.search(r"/drive/folders/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    # Google Docs/Sheets/Slides: .../d/{ID}/...
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    if m:
        return m.group(1)
    return url_or_id


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

async def api_get(
    url: str,
    token: str,
    params: Optional[dict] = None,
) -> tuple[Optional[httpx.Response], Optional[str]]:
    """Authenticated GET. Returns (response, None) or (None, error_message)."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
                timeout=30.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
            return resp, None
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            try:
                api_msg = e.response.json().get("error", {}).get("message", "")
            except Exception:
                api_msg = ""
            detail = api_msg or str(e)

            if status == 401:
                return None, (
                    f"Error 401 Unauthorized: {detail}\n"
                    "Re-authenticate with: gcloud auth login --enable-gdrive-access"
                )
            elif status == 403:
                return None, (
                    f"Error 403 Forbidden: {detail}\n"
                    "Check that the file is shared with you, or enable the API:\n"
                    "  gcloud services enable sheets.googleapis.com drive.googleapis.com"
                )
            elif status == 404:
                return None, (
                    f"Error 404 Not Found: {detail}\n"
                    "Verify the URL/ID is correct and the file has not been deleted."
                )
            else:
                return None, f"Error {status}: {detail}"
        except httpx.TimeoutException:
            return None, "Error: Request timed out. Please try again."


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class ReadSheetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url_or_id: str = Field(
        ...,
        description=(
            "Google Sheets URL (https://docs.google.com/spreadsheets/d/...) "
            "or bare spreadsheet ID"
        ),
        min_length=1,
    )
    sheet_name: Optional[str] = Field(
        default=None,
        description=(
            "Sheet tab name (e.g. 'Sheet1', '売上データ'). "
            "If omitted, the first sheet is used. "
            "Use list_sheets to discover available tab names."
        ),
    )
    range_a1: Optional[str] = Field(
        default=None,
        description=(
            "A1 notation range within the sheet (e.g. 'A1:D100', 'A:C'). "
            "If omitted, all data in the sheet is returned."
        ),
    )


class ListSheetsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url_or_id: str = Field(
        ...,
        description="Google Sheets URL or bare spreadsheet ID",
        min_length=1,
    )


class ReadDriveFileInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url_or_id: str = Field(
        ...,
        description=(
            "Google Drive or Docs URL, or bare file ID. "
            "Supports Google Docs (→ plain text), Sheets (→ CSV), "
            "Slides (→ plain text), and generic files."
        ),
        min_length=1,
    )


class SearchDriveInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(
        ...,
        description="Keyword to match against file names in Google Drive",
        min_length=1,
        max_length=200,
    )
    folder_id: Optional[str] = Field(
        default=None,
        description="Restrict results to files inside this folder (Drive folder ID or URL)",
    )
    include_shared_drives: bool = Field(
        default=True,
        description="Include files from Shared Drives (default: True)",
    )
    max_results: int = Field(
        default=20,
        description="Maximum number of results to return (1–100)",
        ge=1,
        le=100,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="read_sheet",
    annotations={
        "title": "Read Google Sheet data",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def read_sheet(params: ReadSheetInput) -> str:
    """Read cell data from a Google Spreadsheet and return it as tab-separated rows.

    Fetches row data from the specified sheet and range using the Sheets API v4.
    Call list_sheets first if you are unsure of the tab name.

    Args:
        params (ReadSheetInput):
            - url_or_id (str): Spreadsheet URL or ID
            - sheet_name (Optional[str]): Tab name (default: first sheet)
            - range_a1 (Optional[str]): A1 notation range (default: all data)

    Returns:
        str: Tab-separated rows (first row is typically the header), or an error message.

        Success example:
            Name\\tAge\\tCity
            Alice\\t30\\tTokyo
            Bob\\t25\\tOsaka

        Error example:
            "Error 403 Forbidden: ..."
    """
    token, err = await get_access_token()
    if err:
        return err

    spreadsheet_id = extract_spreadsheet_id(params.url_or_id)

    # Resolve range string
    if params.sheet_name and params.range_a1:
        range_str = f"'{params.sheet_name}'!{params.range_a1}"
    elif params.sheet_name:
        range_str = f"'{params.sheet_name}'"
    elif params.range_a1:
        range_str = params.range_a1
    else:
        # Fetch first sheet name
        meta_resp, err = await api_get(
            f"{SHEETS_API}/{spreadsheet_id}",
            token,
            params={"fields": "sheets.properties.title"},
        )
        if err:
            return err
        sheets = meta_resp.json().get("sheets", [])
        if not sheets:
            return "Error: No sheets found in this spreadsheet."
        first_title = sheets[0]["properties"]["title"]
        range_str = f"'{first_title}'"

    resp, err = await api_get(
        f"{SHEETS_API}/{spreadsheet_id}/values/{range_str}",
        token,
    )
    if err:
        return err

    values = resp.json().get("values", [])
    if not values:
        return f"No data found in range {range_str}."

    return "\n".join("\t".join(str(cell) for cell in row) for row in values)


@mcp.tool(
    name="list_sheets",
    annotations={
        "title": "List sheet tabs in a spreadsheet",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def list_sheets(params: ListSheetsInput) -> str:
    """List all sheet tab names and their properties in a Google Spreadsheet.

    Returns the spreadsheet title and an array of sheet objects including
    tab name, row/column counts, and whether the sheet is hidden.

    Args:
        params (ListSheetsInput):
            - url_or_id (str): Spreadsheet URL or ID

    Returns:
        str: JSON object with spreadsheet metadata and sheets array.

        Success schema:
        {
            "spreadsheet_title": str,
            "spreadsheet_id": str,
            "sheets": [
                {
                    "title": str,        # Tab name (use this in read_sheet)
                    "index": int,        # 0-based position
                    "row_count": int,
                    "column_count": int,
                    "hidden": bool
                }
            ]
        }

        Error example: "Error 404 Not Found: ..."
    """
    token, err = await get_access_token()
    if err:
        return err

    spreadsheet_id = extract_spreadsheet_id(params.url_or_id)

    resp, err = await api_get(
        f"{SHEETS_API}/{spreadsheet_id}",
        token,
        params={"fields": "properties.title,sheets.properties"},
    )
    if err:
        return err

    data = resp.json()
    title = data.get("properties", {}).get("title", "")
    sheets = []
    for s in data.get("sheets", []):
        props = s.get("properties", {})
        grid = props.get("gridProperties", {})
        sheets.append({
            "title": props.get("title"),
            "index": props.get("index"),
            "row_count": grid.get("rowCount"),
            "column_count": grid.get("columnCount"),
            "hidden": props.get("hidden", False),
        })

    return json.dumps(
        {
            "spreadsheet_title": title,
            "spreadsheet_id": spreadsheet_id,
            "sheets": sheets,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="read_drive_file",
    annotations={
        "title": "Read or export a Google Drive file",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def read_drive_file(params: ReadDriveFileInput) -> str:
    """Download or export a file from Google Drive as text.

    Export mapping:
    - Google Sheets  → CSV
    - Google Docs    → plain text
    - Google Slides  → plain text
    - Other files    → raw download (decoded as UTF-8)

    Args:
        params (ReadDriveFileInput):
            - url_or_id (str): Drive/Docs URL or file ID

    Returns:
        str: File name as a heading followed by the file content, or an error message.

        Success example:
            # Budget 2025.xlsx
            Month,Revenue,Cost
            January,1000,800
            ...

        Error example: "Error 403 Forbidden: ..."
    """
    token, err = await get_access_token()
    if err:
        return err

    file_id = extract_drive_file_id(params.url_or_id)

    # Fetch metadata to determine MIME type
    meta_resp, err = await api_get(
        f"{DRIVE_API}/files/{file_id}",
        token,
        params={"fields": "id,name,mimeType", "supportsAllDrives": "true"},
    )
    if err:
        return err

    meta = meta_resp.json()
    name = meta.get("name", file_id)
    mime = meta.get("mimeType", "")

    export_mime = GOOGLE_MIME_EXPORT.get(mime)

    if export_mime:
        url = f"{DRIVE_API}/files/{file_id}/export"
        req_params = {"mimeType": export_mime, "supportsAllDrives": "true"}
    else:
        url = f"{DRIVE_API}/files/{file_id}"
        req_params = {"alt": "media", "supportsAllDrives": "true"}

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=req_params,
                timeout=60.0,
                follow_redirects=True,
            )
            resp.raise_for_status()
            return f"# {name}\n\n{resp.text}"
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 403:
                return (
                    f"Error 403 Forbidden accessing '{name}'.\n"
                    "The file may not be shared with you, or the API is not enabled."
                )
            elif status == 404:
                return f"Error 404: File '{file_id}' not found or you lack access."
            return f"Error {status}: {e}"


@mcp.tool(
    name="search_drive",
    annotations={
        "title": "Search Google Drive files by name",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def search_drive(params: SearchDriveInput) -> str:
    """Search Google Drive for files whose names contain the given keyword.

    Returns file IDs, names, MIME types, last-modified times, and web links.
    Results are sorted by most recently modified first.

    Args:
        params (SearchDriveInput):
            - query (str): Keyword to match against file names
            - folder_id (Optional[str]): Restrict to a specific folder
            - include_shared_drives (bool): Include Shared Drive files (default: True)
            - max_results (int): Max results to return (1–100, default: 20)

    Returns:
        str: JSON object with matching files, or an error message.

        Success schema:
        {
            "query": str,
            "count": int,
            "files": [
                {
                    "id": str,
                    "name": str,
                    "mimeType": str,
                    "modifiedTime": str,   # ISO 8601
                    "size": str,           # bytes (absent for Google Workspace files)
                    "webViewLink": str
                }
            ]
        }

        Error example: "Error 403 Forbidden: ..."
    """
    token, err = await get_access_token()
    if err:
        return err

    # Sanitize the query to prevent injection via the Drive API q parameter
    safe_query = params.query.replace("'", "\\'")
    q_parts = [f"name contains '{safe_query}'", "trashed = false"]

    if params.folder_id:
        folder_id = extract_drive_file_id(params.folder_id)
        q_parts.append(f"'{folder_id}' in parents")

    api_params: dict = {
        "q": " and ".join(q_parts),
        "fields": "files(id,name,mimeType,modifiedTime,size,webViewLink)",
        "pageSize": params.max_results,
        "orderBy": "modifiedTime desc",
    }
    if params.include_shared_drives:
        api_params["supportsAllDrives"] = "true"
        api_params["includeItemsFromAllDrives"] = "true"

    resp, err = await api_get(f"{DRIVE_API}/files", token, params=api_params)
    if err:
        return err

    files = resp.json().get("files", [])
    if not files:
        return f"No files found matching '{params.query}'."

    return json.dumps(
        {"query": params.query, "count": len(files), "files": files},
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
