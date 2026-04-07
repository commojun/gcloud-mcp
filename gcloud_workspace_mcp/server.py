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
from enum import Enum
from typing import Any, List, Optional

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

async def get_access_token(account: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """Return (token, None) on success or (None, error_message) on failure."""
    cmd = [GCLOUD_PATH, "auth", "print-access-token"]
    if account:
        cmd.append(f"--account={account}")
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            err = stderr.decode().strip()
            account_hint = f" --account={account}" if account else ""
            return None, (
                f"Error: gcloud auth failed: {err}\n\n"
                "To authenticate, run:\n"
                f"  gcloud auth login{account_hint} --enable-gdrive-access"
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


def col_letter_to_index(letters: str) -> int:
    """Convert column letter(s) to 0-based index. 'A'→0, 'Z'→25, 'AA'→26."""
    result = 0
    for ch in letters.upper():
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result - 1


def col_index_to_letter(index: int) -> str:
    """Convert 0-based column index to letter(s). 0→'A', 25→'Z', 26→'AA'."""
    letters = ""
    n = index + 1
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def parse_range_start(range_str: str) -> tuple[int, int]:
    """Extract (start_row_1indexed, start_col_0indexed) from a range like 'Sheet1!B3:D10' or 'A1'."""
    # Remove sheet name prefix if present
    cell_part = range_str.split("!")[-1]
    # Take the start cell (before ':')
    start_cell = cell_part.split(":")[0]
    # Split letters and digits
    m = re.match(r"([A-Za-z]+)(\d+)", start_cell)
    if not m:
        return 1, 0
    col_idx = col_letter_to_index(m.group(1))
    row_num = int(m.group(2))
    return row_num, col_idx


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


async def api_write(
    url: str,
    token: str,
    method: str,
    params: Optional[dict] = None,
    body: Optional[dict] = None,
) -> tuple[Optional[httpx.Response], Optional[str]]:
    """Authenticated PUT/POST for write operations. Returns (response, None) or (None, error_message)."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
                json=body,
                timeout=30.0,
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
                    "Check that the spreadsheet is shared with edit permission."
                )
            elif status == 400:
                return None, (
                    f"Error 400 Bad Request: {detail}\n"
                    "Check that the range and values dimensions match, "
                    "and that the range notation is correct (e.g. 'A1:C3')."
                )
            else:
                return None, f"Error {status}: {detail}"
        except httpx.TimeoutException:
            return None, "Error: Request timed out. Please try again."


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

ACCOUNT_FIELD = Field(
    default=None,
    description=(
        "Google account email to use (e.g. 'personal@gmail.com'). "
        "If omitted, the default gcloud account is used. "
        "The account must be authenticated via: gcloud auth login --enable-gdrive-access"
    ),
)


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
    show_formulas: bool = Field(
        default=False,
        description=(
            "If True, cells containing formulas return the formula string (e.g. '=SUM(A1:A10)') "
            "instead of the calculated value. Use this when you need to understand or replicate "
            "existing formulas before editing. "
            "Limitation: ARRAYFORMULA spill cells cannot be distinguished from plain value cells — "
            "they return the calculated value regardless. Always check the formula in the first "
            "cell of a range before writing to spill cells."
        ),
    )
    account: Optional[str] = ACCOUNT_FIELD


class ListSheetsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url_or_id: str = Field(
        ...,
        description="Google Sheets URL or bare spreadsheet ID",
        min_length=1,
    )
    account: Optional[str] = ACCOUNT_FIELD


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
    account: Optional[str] = ACCOUNT_FIELD


class SearchDriveInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: Optional[str] = Field(
        default=None,
        description=(
            "Keyword to match against file names in Google Drive. "
            "If omitted or empty, all items in the specified folder are returned "
            "(folder_id must be provided in that case)."
        ),
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
    account: Optional[str] = ACCOUNT_FIELD


class ValueInputOption(str, Enum):
    USER_ENTERED = "USER_ENTERED"
    RAW = "RAW"


class UpdateSheetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url_or_id: str = Field(
        ...,
        description="Spreadsheet URL or ID",
        min_length=1,
    )
    range_a1: str = Field(
        ...,
        description=(
            "A1 notation of the range to write (e.g. 'B3', 'A1:C3'). "
            "Include sheet name prefix if needed (e.g. 'Sheet1!A1:C3'). "
            "The values array dimensions must match this range."
        ),
        min_length=1,
    )
    values: List[List[Any]] = Field(
        ...,
        description=(
            "2D array of values to write. Outer list = rows, inner list = cells left to right. "
            "Example: [['Alice', 30, 'Tokyo'], ['Bob', 25, 'Osaka']] writes 2 rows × 3 columns."
        ),
    )
    sheet_name: Optional[str] = Field(
        default=None,
        description=(
            "Sheet tab name. Prepended to range_a1 if range_a1 does not already contain '!'. "
            "If omitted and range_a1 has no sheet prefix, the API uses the first sheet."
        ),
    )
    value_input_option: ValueInputOption = Field(
        default=ValueInputOption.USER_ENTERED,
        description=(
            "USER_ENTERED (default): values are interpreted as if typed by a user — "
            "formulas starting with '=' are evaluated, dates are parsed. "
            "RAW: values are stored exactly as provided, no interpretation."
        ),
    )
    account: Optional[str] = ACCOUNT_FIELD


class AppendRowsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url_or_id: str = Field(
        ...,
        description="Spreadsheet URL or ID",
        min_length=1,
    )
    values: List[List[Any]] = Field(
        ...,
        description=(
            "2D array of rows to append after the last row that has data. "
            "Each inner list is one row. "
            "Example: [['Alice', 30, 'Tokyo']] appends one row."
        ),
    )
    sheet_name: Optional[str] = Field(
        default=None,
        description="Sheet tab name to append to. If omitted, appends to the first sheet.",
    )
    value_input_option: ValueInputOption = Field(
        default=ValueInputOption.USER_ENTERED,
        description=(
            "USER_ENTERED (default): formulas and dates are interpreted. "
            "RAW: values stored exactly as provided."
        ),
    )
    account: Optional[str] = ACCOUNT_FIELD


class ClearRangeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    url_or_id: str = Field(
        ...,
        description="Spreadsheet URL or ID",
        min_length=1,
    )
    range_a1: str = Field(
        ...,
        description=(
            "A1 notation of the range to clear (e.g. 'B3:D10', 'A5'). "
            "Include sheet name prefix if needed (e.g. 'Sheet1!A1:Z100'). "
            "Clearing removes values but preserves formatting."
        ),
        min_length=1,
    )
    sheet_name: Optional[str] = Field(
        default=None,
        description=(
            "Sheet tab name. Prepended to range_a1 if range_a1 does not already contain '!'."
        ),
    )
    account: Optional[str] = ACCOUNT_FIELD


# ---------------------------------------------------------------------------
# Tools


@mcp.tool(
    name="list_accounts",
    annotations={
        "title": "List authenticated gcloud accounts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def list_accounts() -> str:
    """List all Google accounts authenticated with gcloud.

    Use this to discover available account emails before calling other tools
    with the account parameter.

    Returns:
        str: JSON array of authenticated accounts.

        Success schema:
        [
            {
                "account": str,   # Email address
                "active": bool    # True if this is the current default account
            }
        ]

        Error example: "Error: gcloud auth failed: ..."
    """
    try:
        process = await asyncio.create_subprocess_exec(
            GCLOUD_PATH,
            "auth",
            "list",
            "--format=json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            err = stderr.decode().strip()
            return (
                f"Error: gcloud auth failed: {err}\n\n"
                "To authenticate, run:\n"
                "  gcloud auth login --enable-gdrive-access"
            )
        raw = json.loads(stdout.decode())
        accounts = [
            {
                "account": entry.get("account"),
                "active": entry.get("status") == "ACTIVE",
            }
            for entry in raw
        ]
        return json.dumps(accounts, ensure_ascii=False, indent=2)
    except FileNotFoundError:
        return (
            f"Error: gcloud CLI not found at '{GCLOUD_PATH}'.\n"
            "Install from https://cloud.google.com/sdk/docs/install\n"
            "Or set the GCLOUD_PATH environment variable to the full path."
        )
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
    """Read cell data from a Google Spreadsheet with exact cell address information.

    Returns a JSON object where every cell is mapped to its A1-notation address
    (e.g. "B3"). Empty cells are included explicitly so callers can identify
    blank cells by position. This is essential for pinpoint cell editing.

    Args:
        params (ReadSheetInput):
            - url_or_id (str): Spreadsheet URL or ID
            - sheet_name (Optional[str]): Tab name (default: first sheet)
            - range_a1 (Optional[str]): A1 notation range (default: all data)

    Returns:
        str: JSON object with the following schema:

        {
            "range": str,          # Actual range returned (e.g. "Sheet1!A1:D5")
            "sheet": str,          # Sheet tab name
            "row_count": int,      # Number of data rows
            "col_count": int,      # Number of columns (max width)
            "rows": [
                {
                    "row": int,            # 1-based row number in the spreadsheet
                    "cells": {
                        "A": str,          # Cell value (empty string if blank)
                        "B": str,
                        ...
                    }
                }
            ]
        }

        To edit a specific cell, use the "row" number and column letter
        to construct the A1 address (e.g. row=3, col="B" → "B3").

        Error example: "Error 403 Forbidden: ..."
    """
    token, err = await get_access_token(params.account)
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

    render_option = "FORMULA" if params.show_formulas else "UNFORMATTED_VALUE"
    resp, err = await api_get(
        f"{SHEETS_API}/{spreadsheet_id}/values/{range_str}",
        token,
        params={"valueRenderOption": render_option},
    )
    if err:
        return err

    body = resp.json()
    values = body.get("values", [])
    if not values:
        return f"No data found in range {range_str}."

    actual_range = body.get("range", range_str)
    # Extract sheet name from "SheetName!A1:D5" format
    sheet_label = actual_range.split("!")[0].strip("'") if "!" in actual_range else ""

    start_row, start_col_idx = parse_range_start(actual_range)
    max_cols = max(len(row) for row in values)

    rows = []
    for i, row in enumerate(values):
        # Pad shorter rows with empty strings so every column is present
        padded = row + [""] * (max_cols - len(row))
        cells = {
            col_index_to_letter(start_col_idx + j): str(v)
            for j, v in enumerate(padded)
        }
        rows.append({"row": start_row + i, "cells": cells})

    result: dict = {
        "range": actual_range,
        "sheet": sheet_label,
        "row_count": len(rows),
        "col_count": max_cols,
        "show_formulas": params.show_formulas,
        "rows": rows,
    }
    if params.show_formulas:
        result["formula_note"] = (
            "Cells with formulas show the formula string (e.g. '=SUM(...)'). "
            "Cells without formulas show the calculated value. "
            "ARRAYFORMULA spill cells (row 2+) are indistinguishable from plain values."
        )

    return json.dumps(result, ensure_ascii=False, indent=2)


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
    token, err = await get_access_token(params.account)
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
    token, err = await get_access_token(params.account)
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

    If query is omitted or empty, all items in the specified folder are returned
    (folder_id must be provided in that case).

    Args:
        params (SearchDriveInput):
            - query (Optional[str]): Keyword to match against file names.
              If empty, all items in folder_id are returned.
            - folder_id (Optional[str]): Restrict to a specific folder
            - include_shared_drives (bool): Include Shared Drive files (default: True)
            - max_results (int): Max results to return (1–100, default: 20)

    Returns:
        str: JSON object with matching files, or an error message.

        Success schema:
        {
            "query": str,            # omitted if no query was given
            "folder_id": str,        # omitted if no folder was specified
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
    token, err = await get_access_token(params.account)
    if err:
        return err

    has_query = bool(params.query and params.query.strip())
    if not has_query and not params.folder_id:
        if params.include_shared_drives:
            return "Error: Either query or folder_id must be provided."
        # include_shared_drives=False → default to My Drive root
        effective_folder_id = "root"
    else:
        effective_folder_id = extract_drive_file_id(params.folder_id) if params.folder_id else None

    q_parts = ["trashed = false"]

    if has_query:
        safe_query = params.query.replace("'", "\\'")
        q_parts.append(f"name contains '{safe_query}'")

    if effective_folder_id:
        q_parts.append(f"'{effective_folder_id}' in parents")

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
        if has_query:
            return f"No files found matching '{params.query}'."
        return "No files found in the specified folder."

    result: dict = {"count": len(files), "files": files}
    if has_query:
        result["query"] = params.query
    if effective_folder_id:
        result["folder_id"] = effective_folder_id

    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

def _build_write_range(range_a1: str, sheet_name: Optional[str]) -> str:
    """Prepend sheet name to range if not already embedded."""
    if "!" in range_a1:
        return range_a1
    if sheet_name:
        return f"'{sheet_name}'!{range_a1}"
    return range_a1


@mcp.tool(
    name="update_sheet",
    annotations={
        "title": "Write values to a Google Sheet range",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def update_sheet(params: UpdateSheetInput) -> str:
    """Overwrite a range in a Google Spreadsheet with the given values.

    Writes a 2D array of values into the specified A1 range. Existing content
    in the range is replaced. Use read_sheet first to verify the target range
    before writing, especially when the sheet contains formulas.

    Args:
        params (UpdateSheetInput):
            - url_or_id (str): Spreadsheet URL or ID
            - range_a1 (str): A1 notation range to write (e.g. 'B3', 'A1:C3')
            - values (List[List[Any]]): 2D array — rows × cells
            - sheet_name (Optional[str]): Tab name (prepended to range if needed)
            - value_input_option (ValueInputOption): USER_ENTERED or RAW (default: USER_ENTERED)

    Returns:
        str: JSON with update summary, or an error message.

        Success schema:
        {
            "updated_range": str,   # Actual range that was updated
            "updated_rows": int,
            "updated_columns": int,
            "updated_cells": int
        }

        Error example: "Error 400 Bad Request: ..."
    """
    token, err = await get_access_token(params.account)
    if err:
        return err

    spreadsheet_id = extract_spreadsheet_id(params.url_or_id)
    range_str = _build_write_range(params.range_a1, params.sheet_name)

    resp, err = await api_write(
        f"{SHEETS_API}/{spreadsheet_id}/values/{range_str}",
        token,
        method="PUT",
        params={"valueInputOption": params.value_input_option.value},
        body={"range": range_str, "majorDimension": "ROWS", "values": params.values},
    )
    if err:
        return err

    data = resp.json()
    return json.dumps(
        {
            "updated_range": data.get("updatedRange"),
            "updated_rows": data.get("updatedRows"),
            "updated_columns": data.get("updatedColumns"),
            "updated_cells": data.get("updatedCells"),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="append_rows",
    annotations={
        "title": "Append rows to a Google Sheet",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def append_rows(params: AppendRowsInput) -> str:
    """Append one or more rows after the last row that contains data.

    Rows are inserted immediately after the last non-empty row in the sheet,
    regardless of which range is specified internally. Existing data is never
    overwritten. Each call appends a new set of rows.

    Args:
        params (AppendRowsInput):
            - url_or_id (str): Spreadsheet URL or ID
            - values (List[List[Any]]): 2D array of rows to append
            - sheet_name (Optional[str]): Tab name (default: first sheet)
            - value_input_option (ValueInputOption): USER_ENTERED or RAW (default: USER_ENTERED)

    Returns:
        str: JSON with append summary, or an error message.

        Success schema:
        {
            "updated_range": str,   # Range where rows were appended
            "updated_rows": int,
            "updated_columns": int,
            "updated_cells": int
        }

        Error example: "Error 403 Forbidden: ..."
    """
    token, err = await get_access_token(params.account)
    if err:
        return err

    spreadsheet_id = extract_spreadsheet_id(params.url_or_id)

    # Resolve sheet name for the append anchor range
    if params.sheet_name:
        anchor = f"'{params.sheet_name}'"
    else:
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
        anchor = f"'{sheets[0]['properties']['title']}'"

    resp, err = await api_write(
        f"{SHEETS_API}/{spreadsheet_id}/values/{anchor}:append",
        token,
        method="POST",
        params={
            "valueInputOption": params.value_input_option.value,
            "insertDataOption": "INSERT_ROWS",
        },
        body={"majorDimension": "ROWS", "values": params.values},
    )
    if err:
        return err

    updates = resp.json().get("updates", {})
    return json.dumps(
        {
            "updated_range": updates.get("updatedRange"),
            "updated_rows": updates.get("updatedRows"),
            "updated_columns": updates.get("updatedColumns"),
            "updated_cells": updates.get("updatedCells"),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool(
    name="clear_range",
    annotations={
        "title": "Clear a range in a Google Sheet",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def clear_range(params: ClearRangeInput) -> str:
    """Clear all values in the specified range of a Google Spreadsheet.

    Removes cell values but preserves formatting (colors, borders, etc.).
    This operation is irreversible — use with caution.

    Args:
        params (ClearRangeInput):
            - url_or_id (str): Spreadsheet URL or ID
            - range_a1 (str): A1 notation of the range to clear (e.g. 'B3:D10')
            - sheet_name (Optional[str]): Tab name (prepended to range if needed)

    Returns:
        str: JSON with cleared range info, or an error message.

        Success schema:
        {
            "cleared_range": str,
            "spreadsheet_id": str
        }

        Error example: "Error 403 Forbidden: ..."
    """
    token, err = await get_access_token(params.account)
    if err:
        return err

    spreadsheet_id = extract_spreadsheet_id(params.url_or_id)
    range_str = _build_write_range(params.range_a1, params.sheet_name)

    resp, err = await api_write(
        f"{SHEETS_API}/{spreadsheet_id}/values/{range_str}:clear",
        token,
        method="POST",
    )
    if err:
        return err

    data = resp.json()
    return json.dumps(
        {
            "cleared_range": data.get("clearedRange"),
            "spreadsheet_id": data.get("spreadsheetId"),
        },
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
