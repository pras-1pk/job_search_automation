import io
import logging

import pdfplumber
from docx import Document
from google.auth import default
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

# Google Drive mime types
MIME_PDF    = "application/pdf"
MIME_GDOC   = "application/vnd.google-apps.document"
MIME_DOCX   = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _get_drive_service():
    creds, _ = default(scopes=["https://www.googleapis.com/auth/drive.readonly"])
    return build("drive", "v3", credentials=creds)


def fetch_resume_text(file_id: str) -> str:
    """
    Download resume from Google Drive and return plain text.
    Supports: PDF, DOCX, Google Docs.
    """
    service = _get_drive_service()

    # Get file metadata
    meta = service.files().get(fileId=file_id, fields="name,mimeType").execute()
    mime_type = meta["mimeType"]
    logger.info(f"Downloading resume: '{meta['name']}' (type={mime_type})")

    buffer = io.BytesIO()

    if mime_type == MIME_GDOC:
        # Export Google Docs as PDF
        request = service.files().export_media(fileId=file_id, mimeType=MIME_PDF)
    else:
        request = service.files().get_media(fileId=file_id)

    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    buffer.seek(0)

    if mime_type in (MIME_PDF, MIME_GDOC):
        return _parse_pdf(buffer)
    elif mime_type == MIME_DOCX:
        return _parse_docx(buffer)
    else:
        raise ValueError(f"Unsupported resume format: {mime_type}. Use PDF, DOCX, or Google Docs.")


def _parse_pdf(buffer: io.BytesIO) -> str:
    """Extract text from PDF using pdfplumber (handles multi-column layouts well)."""
    text_parts = []
    with pdfplumber.open(buffer) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    result = "\n".join(text_parts).strip()
    if not result:
        raise ValueError("Could not extract text from resume PDF. Is it a scanned image?")
    return result


def _parse_docx(buffer: io.BytesIO) -> str:
    """Extract text from DOCX."""
    doc = Document(buffer)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip()).strip()