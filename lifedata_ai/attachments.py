"""Convert chat attachments to persisted multimodal message blocks."""
from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MAX_FILES = 4
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 15 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
TEXT_EXTENSIONS = {'.txt', '.md', '.csv', '.tsv', '.json', '.log', '.yaml', '.yml'}


class AttachmentError(ValueError):
    """An attachment cannot be interpreted by this build."""


def _office_text(data: bytes, extension: str) -> str:
    """Extract document paragraphs or spreadsheet cells without executing content."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = ['word/document.xml'] if extension == '.docx' else [
                name for name in archive.namelist()
                if name == 'xl/sharedStrings.xml' or name.startswith('xl/worksheets/sheet')
            ]
            if sum(archive.getinfo(name).file_size for name in names) > MAX_FILE_BYTES:
                raise AttachmentError('The expanded document is too large. Export a smaller file.')
            shared: list[str] = []
            pieces: list[str] = []
            for name in sorted(names, key=lambda n: (n != 'xl/sharedStrings.xml', n)):
                root = ElementTree.fromstring(archive.read(name))
                if name == 'xl/sharedStrings.xml':
                    shared = [''.join(item.itertext()) for item in root]
                elif extension == '.docx':
                    pieces.extend(''.join(node.itertext()) for node in root.iter() if node.tag.endswith('}p'))
                else:
                    pieces.append(f'Sheet: {name}')
                    for row in root.iter():
                        if not row.tag.endswith('}row'):
                            continue
                        cells = []
                        for cell in row:
                            value = ''.join(cell.itertext())
                            if cell.attrib.get('t') == 's' and value.isdigit():
                                value = shared[int(value)] if int(value) < len(shared) else value
                            cells.append(f"{cell.attrib.get('r', '')}: {value}")
                        pieces.append('\t'.join(cells))
            return '\n'.join(pieces)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise AttachmentError('This Office file is malformed. Export it as PDF or CSV.') from exc


def attachment_blocks(files: list[Any]) -> list[dict[str, Any]]:
    """Validate uploaded files and preserve image/PDF inputs for model vision."""
    if len(files) > MAX_FILES:
        raise AttachmentError(f'Attach at most {MAX_FILES} files per message.')
    if sum(upload.size for upload in files) > MAX_TOTAL_BYTES:
        raise AttachmentError("Attachments must total 15 MB or less per message.")
    blocks: list[dict[str, Any]] = []
    for upload in files:
        name = Path(upload.name).name[:200]
        if upload.size > MAX_FILE_BYTES:
            raise AttachmentError(f'{name} exceeds the 5 MB per-file limit.')
        data = upload.read(MAX_FILE_BYTES + 1)
        if not data or len(data) > MAX_FILE_BYTES:
            raise AttachmentError(f'{name} is empty or exceeds the 5 MB per-file limit.')
        media = None
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            media = 'image/png'
        elif data.startswith(b'\xff\xd8\xff'):
            media = 'image/jpeg'
        elif data[:6] in (b'GIF87a', b'GIF89a'):
            media = 'image/gif'
        elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
            media = 'image/webp'
        elif data.startswith(b'%PDF-'):
            media = 'application/pdf'
        if media:
            blocks.append({'type': 'text', 'text': f'Attached file: {name}'})
            block: dict[str, Any] = {
                'type': 'document' if media == 'application/pdf' else 'image',
                'source': {'type': 'base64', 'media_type': media,
                           'data': base64.b64encode(data).decode('ascii')},
            }
            if media == 'application/pdf':
                block['title'] = name
            blocks.append(block)
            continue
        extension = Path(name).suffix.lower()
        if extension in {'.docx', '.xlsx'}:
            text = _office_text(data, extension)
        elif extension in TEXT_EXTENSIONS:
            try:
                text = data.decode('utf-8-sig')
            except UnicodeDecodeError as exc:
                raise AttachmentError(f'{name} must use UTF-8 text encoding.') from exc
        else:
            raise AttachmentError(f'{name}: use PNG, JPEG, GIF, WebP, PDF, DOCX, XLSX, CSV or a text file.')
        if not text.strip():
            raise AttachmentError(f'{name} contains no readable text.')
        truncated = len(text) > MAX_TEXT_CHARS
        blocks.append({'type': 'text', 'text': f'Attached file: {name}\n'
                       + ('[Excerpt: file text truncated]\n' if truncated else '')
                       + text[:MAX_TEXT_CHARS]})
    return blocks
