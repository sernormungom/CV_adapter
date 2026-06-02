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

    doc = Document(str(file_path))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)
