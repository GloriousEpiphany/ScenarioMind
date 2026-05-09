"""Deterministic report exporters for enterprise Scenario Lab reports."""

from __future__ import annotations

import hashlib
import html
import re
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from .schemas import ReportExport


class ReportExporter:
    SUPPORTED_FORMATS = {"pdf", "docx"}

    def export(self, report_id: str, markdown: str, export_format: str, output_dir: str | Path) -> ReportExport:
        fmt = export_format.lower().strip()
        if fmt not in self.SUPPORTED_FORMATS:
            raise ValueError("export_format must be one of: pdf, docx")
        if not markdown.strip():
            raise ValueError("markdown content is required for export")
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        content_hash = hashlib.sha1(markdown.encode("utf-8")).hexdigest()
        seed = f"{report_id}|{fmt}|{content_hash}|{datetime.now().isoformat(timespec='microseconds')}"
        export_id = f"export_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"
        path = directory / f"{report_id}_{export_id}.{fmt}"
        if fmt == "pdf":
            self._write_pdf(path, markdown)
        else:
            self._write_docx(path, markdown)
        return ReportExport(
            export_id=export_id,
            report_id=report_id,
            export_format=fmt,
            file_path=str(path),
            content_hash=content_hash,
            created_at=datetime.now().isoformat(timespec="seconds"),
            audit={
                "method": "deterministic_local_report_export",
                "source": "persisted_markdown_report",
                "validation_status": "passed",
            },
        )

    def _write_pdf(self, path: Path, markdown: str) -> None:
        text = self._plain_text(markdown)
        safe_lines = [line for line in text.splitlines()]
        content_lines = ["BT", "/F1 10 Tf", "50 780 Td", "14 TL"]
        for line in safe_lines[:50]:
            encoded = line[:100].encode("utf-16-be").hex().upper()
            content_lines.append(f"<FEFF{encoded}> Tj")
            content_lines.append("T*")
        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("ascii")
        objects = [
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
            b"4 0 obj << /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H /DescendantFonts [6 0 R] >> endobj\n",
            b"5 0 obj << /Length " + str(len(stream)).encode("ascii") + b" >> stream\n" + stream + b"\nendstream endobj\n",
            b"6 0 obj << /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> >> endobj\n",
        ]
        data = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for obj in objects:
            offsets.append(len(data))
            data.extend(obj)
        xref_offset = len(data)
        data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
        for offset in offsets[1:]:
            data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        data.extend(f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
        path.write_bytes(bytes(data))

    def _write_docx(self, path: Path, markdown: str) -> None:
        paragraphs = [
            f"<w:p><w:r><w:t>{escape(line) or ' '}</w:t></w:r></w:p>"
            for line in self._plain_text(markdown).splitlines()
        ]
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{''.join(paragraphs)}<w:sectPr/></w:body></w:document>"
        )
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", self._content_types())
            zf.writestr("_rels/.rels", self._rels())
            zf.writestr("word/document.xml", document)

    def _plain_text(self, markdown: str) -> str:
        text = re.sub(r"`([^`]*)`", r"\1", markdown)
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
        return html.unescape(text)

    def _content_types(self) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>"
        )

    def _rels(self) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            "</Relationships>"
        )
