"""Uploads retain visual content, reject unsupported files and extract Office text."""
import base64
import io
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from lifedata_ai.attachments import MAX_FILE_BYTES, AttachmentError, attachment_blocks

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')


def test_image_keeps_bytes_and_uses_content_signature():
    blocks = attachment_blocks([SimpleUploadedFile('screen.png', PNG, content_type='text/plain')])
    assert blocks[0]['text'] == 'Attached file: screen.png'
    assert blocks[1]['type'] == 'image'
    assert blocks[1]['source']['media_type'] == 'image/png'
    assert base64.b64decode(blocks[1]['source']['data']) == PNG


def test_pdf_is_native_document():
    blocks = attachment_blocks([SimpleUploadedFile('report.pdf', b'%PDF-1.7\n')])
    assert blocks[1]['type'] == 'document'
    assert blocks[1]['title'] == 'report.pdf'


@pytest.mark.parametrize('name,data', [('malware.exe', b'bin'), ('data.csv', b'\xff'), ('empty.txt', b'')])
def test_unsupported_or_unreadable_file_is_reported(name, data):
    with pytest.raises(AttachmentError):
        attachment_blocks([SimpleUploadedFile(name, data)])


def test_size_and_count_limits():
    with pytest.raises(AttachmentError):
        attachment_blocks([SimpleUploadedFile('big.txt', b'x' * (MAX_FILE_BYTES + 1))])
    with pytest.raises(AttachmentError):
        attachment_blocks([SimpleUploadedFile('a.txt', b'hello') for _ in range(5)])


@pytest.mark.parametrize('extension,path,xml,expected', [
    ('.docx', 'word/document.xml', '<document xmlns="urn:word"><p><r><t>Report summary</t></r></p></document>', 'Report summary'),
    ('.xlsx', 'xl/worksheets/sheet1.xml', '<worksheet xmlns="urn:sheet"><row><c r="A1"><v>299</v></c></row></worksheet>', 'A1: 299'),
])
def test_office_files_extract_readable_text(extension, path, xml, expected):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr(path, xml)
    blocks = attachment_blocks([SimpleUploadedFile('file' + extension, buffer.getvalue())])
    assert expected in blocks[0]['text']
