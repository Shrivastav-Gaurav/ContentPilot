"""Load the user's resume as plain text (headings and bullets kept).

Order: data/resume.md, else the newest data/*.docx (python-docx). An old .doc is
not readable here: the caller shows a notice and skips fit scoring.

The resume text is personal: never log or print it. Only the fit call and the
Apply pack use it.
"""
import glob
import hashlib
import os
from dataclasses import dataclass

DOC_NOTICE = "Save your resume as .docx in Word"
MISSING_NOTICE = "No resume found. Add data/resume.md or a .docx resume to the data folder to score jobs."


@dataclass(frozen=True)
class Resume:
    text: str | None
    source: str | None     # file name only, never the content
    notice: str | None     # shown instead of scores when text is None

    @property
    def ok(self) -> bool:
        return bool(self.text)

    @property
    def hash(self) -> str | None:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16] if self.text else None


def _is_list_paragraph(p) -> bool:
    ppr = p._p.pPr
    return (ppr is not None and ppr.numPr is not None) or p.style.name.lower().startswith("list")


def _heading_level(p) -> int | None:
    name = p.style.name.lower()
    if name == "title":
        return 1
    if name.startswith("heading"):
        digits = "".join(ch for ch in name if ch.isdigit())
        return min(int(digits), 3) if digits else 2
    # Many resumes use short bold Normal paragraphs as section headings.
    text = p.text.strip()
    runs = [r for r in p.runs if r.text.strip()]
    if runs and len(text) <= 60 and all(r.bold for r in runs) and not text.endswith("."):
        return 2
    return None


def docx_to_text(path: str) -> str:
    import docx  # python-docx

    document = docx.Document(path)
    lines: list[str] = []
    for p in document.paragraphs:
        text = " ".join(p.text.split())
        if not text:
            continue
        level = _heading_level(p)
        if _is_list_paragraph(p):
            lines.append(f"- {text}")
        elif level:
            if lines:
                lines.append("")
            lines.append(f"{'#' * level} {text}")
        else:
            lines.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [" ".join(c.text.split()) for c in row.cells]
            cells = [c for i, c in enumerate(cells) if c and c not in cells[:i]]  # merged cells repeat
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines).strip()


def load_resume(data_dir: str) -> Resume:
    md_path = os.path.join(data_dir, "resume.md")
    if os.path.isfile(md_path):
        with open(md_path, encoding="utf-8") as f:
            text = f.read().strip()
        return Resume(text or None, "resume.md", None if text else MISSING_NOTICE)

    docx_files = [f for f in glob.glob(os.path.join(data_dir, "*.docx"))
                  if not os.path.basename(f).startswith("~$")]  # Word lock files
    if docx_files:
        newest = max(docx_files, key=os.path.getmtime)
        try:
            text = docx_to_text(newest)
        except Exception as exc:  # corrupt file: say so without echoing content
            return Resume(None, os.path.basename(newest), f"Could not read {os.path.basename(newest)}: {type(exc).__name__}")
        return Resume(text or None, os.path.basename(newest), None if text else MISSING_NOTICE)

    if glob.glob(os.path.join(data_dir, "*.doc")):
        return Resume(None, None, DOC_NOTICE)
    return Resume(None, None, MISSING_NOTICE)
