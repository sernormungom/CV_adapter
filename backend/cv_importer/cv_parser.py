import tempfile
from pathlib import Path


def extract_text(file_path: Path, mime_type: str) -> str:
    if mime_type == "application/pdf":
        return _extract_pdf(file_path)
    if mime_type in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        return _extract_docx(file_path)
    raise ValueError(f"Unsupported file type: {mime_type}")


def _extract_pdf(file_path: Path) -> str:
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=2, y_tolerance=2)
            if text:
                pages.append(text.strip())
    return "\n\n".join(pages)


def _extract_docx(file_path: Path) -> str:
    from docx import Document
    from docx.oxml.ns import qn

    doc = Document(str(file_path))

    # Walk the document body in document order, collecting text from both
    # paragraphs and tables (many Word CVs put all content inside tables).
    body = doc.element.body
    chunks: list[str] = []

    for child in body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag

        if tag == "p":
            text = "".join(n.text or "" for n in child.iter(qn("w:t"))).strip()
            if text:
                chunks.append(text)

        elif tag == "tbl":
            # Collect unique cell texts in reading order; skip duplicate merged cells
            seen: set[str] = set()
            row_texts: list[str] = []
            for row in child.iter(qn("w:tr")):
                cell_texts: list[str] = []
                for cell in row.iter(qn("w:tc")):
                    cell_text = "".join(
                        n.text or "" for n in cell.iter(qn("w:t"))
                    ).strip()
                    if cell_text and cell_text not in seen:
                        seen.add(cell_text)
                        cell_texts.append(cell_text)
                if cell_texts:
                    row_texts.append(" | ".join(cell_texts))
            if row_texts:
                chunks.append("\n".join(row_texts))

    return "\n\n".join(chunks)
