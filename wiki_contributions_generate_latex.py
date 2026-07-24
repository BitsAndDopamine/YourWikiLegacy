#!/usr/bin/env python3
"""
wiki_contributions_generate_latex.py
======================================
Generates a print-ready LaTeX document from the wiki_contributions
SQLite database: "Every article I have ever edited."

Usage:
    python wiki_contributions_generate_latex.py --db my_contributions.db --out my_book.tex
    python wiki_contributions_generate_latex.py --db my_contributions.db --out my_book.tex --group-by lang
    python wiki_contributions_generate_latex.py --db my_contributions.db --out my_book.tex --limit 200
    xelatex my_book.tex
"""

import argparse
import re
import sqlite3
from pathlib import Path

# ─── LaTeX helper functions ────────────────────────────────────────────────

LATEX_SPECIAL = {
    "&":  r"\&",
    "%":  r"\%",
    "$":  r"\$",
    "#":  r"\#",
    "_":  r"\_",
    "{":  r"\{",
    "}":  r"\}",
    "~":  r"\textasciitilde{}",
    "^":  r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
}


def latex_escape(text: str) -> str:
    if not text:
        return ""
    return "".join(LATEX_SPECIAL.get(ch, ch) for ch in text)


def first_letter(sort_key: str) -> str:
    """Section letter: first character of the (already diacritic-normalized)
    sort_key. Non-alphabetic titles land under '0-9'."""
    if not sort_key:
        return r"\#"
    ch = sort_key[0]
    if ch.isalpha():
        return ch
    return r"\#"


def normalize_text(text: str, cut: bool = True, cut_length: int = 280) -> str:
    if not text:
        return ""
    text = re.sub(r"\n{2,}", " / ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if cut and len(text) > cut_length:
        cut_text = text[:cut_length]
        last_period = max(cut_text.rfind("."), cut_text.rfind("!"), cut_text.rfind("?"))
        if last_period > cut_length // 2:
            text = cut_text[:last_period + 1]
        else:
            last_space = cut_text.rfind(" ")
            text = cut_text[:last_space].rstrip() + " …"
    return text

# ─── LaTeX document ────────────────────────────────────────────────────────

# Three-column layout needs a smaller font size than a two-column one to
# keep line lengths readable, following the same approach as the original
# Esperanto encyclopedia project.
PREAMBLE = r"""\documentclass[7pt, a4paper, twoside]{article}

\usepackage{fontspec}
\usepackage{polyglossia}
\setmainlanguage{english}

\setmainfont{TeX Gyre Pagella}
\newfontfamily\unifontfont{Unifont}
\usepackage[
  top=1.5cm, bottom=1.8cm,
  inner=1.4cm, outer=1.2cm,
  headsep=4mm
]{geometry}
\usepackage{multicol}
\usepackage{microtype}
\usepackage{fancyhdr}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{xcolor}
\usepackage{colortbl}

\definecolor{infohead}{RGB}{220,230,242}
\definecolor{infoline}{RGB}{170,190,210}
\definecolor{langtag}{RGB}{120,120,120}

\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\setlength{\columnsep}{3.5mm}
\setlength{\columnseprule}{0.25pt}
\setlength{\baselineskip}{10pt}

\pagestyle{fancy}
\fancyhf{}
\fancyhead[LE,RO]{\small\textbf{\leftmark}}
\fancyhead[RE,LO]{\small\itshape __HEADER_TITLE__}
\fancyfoot[C]{\small\thepage}
\renewcommand{\headrulewidth}{0.4pt}

% Entry: #1 = title, #2 = language code, #3 = text
\newcommand{\entry}[3]{%
  \markboth{#1}{#1}%
  \noindent{\small\textbf{#1}}\,{\tiny\color{langtag}[#2]}\par{\small\unifontfont #3}\par%
  \vspace{2pt}%
}

\newcommand{\entryimage}[2]{%
  \begin{center}%
    \includegraphics[width=0.85\linewidth]{#1}\par\vspace{1pt}%
    {\tiny\itshape #2}%
  \end{center}\vspace{2pt}%
}

\newenvironment{infobox}[1]{%
  \vspace{2pt}%
  \setlength{\arrayrulewidth}{0.3pt}%
  \arrayrulecolor{infoline}%
  \begin{center}%
  \begin{tabular}{@{}p{0.342\linewidth}p{0.495\linewidth}@{}}%
  \multicolumn{2}{@{}p{0.9\linewidth}@{}}{\cellcolor{infohead}\hspace{2pt}{\tiny\textbf{#1}}}\\[-1pt]\hline%
}{%
  \hline\end{tabular}\end{center}\vspace{1pt}%
}
\newcommand{\inforow}[2]{{\tiny\textit{#1}} & {\tiny #2}\\}

\newcommand{\lettersection}[1]{%
  \vspace{4pt}%
  \noindent\rule{\linewidth}{0.4pt}\par%
  \noindent{\footnotesize\textbf{#1}}\par%
  \noindent\rule{\linewidth}{0.4pt}\par%
  \vspace{2pt}%
}

\newcommand{\langsection}[1]{%
  \clearpage
  \vspace{4pt}%
  {\large\textbf{#1}}\par%
  \vspace{5pt}%
}

\title{__DOC_TITLE__}
\author{__DOC_AUTHOR__}
\date{}

\begin{document}
\maketitle
\begin{multicols}{3}
\raggedcolumns
"""

POSTAMBLE = r"""
\end{multicols}
\end{document}
"""

# Full language code -> display name mapping for --group-by lang (most common wikis; extendable)
LANG_NAMES = {
    "en": "English", "de": "Deutsch", "eo": "Esperanto", "fr": "Français",
    "es": "Español", "it": "Italiano", "nl": "Nederlands", "pl": "Polski",
    "ru": "Русский", "pt": "Português", "sv": "Svenska", "ja": "日本語",
    "zh": "中文", "ar": "العربية", "fi": "Suomi", "no": "Norsk", "da": "Dansk",
}


def lang_display(code: str) -> str:
    return LANG_NAMES.get(code, code.upper())


# ─── Main logic ────────────────────────────────────────────────────────────

def load_articles(db_path: str, group_by: str, status: str | None, limit: int | None) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT title, wiki_lang, wiki_project, text, text_edited, extra_latex,
               sort_key, first_edited_at, last_edited_at
        FROM articles
        WHERE (text IS NOT NULL AND text != '')
          AND title IS NOT NULL
          AND (comment IS NULL OR comment != 'duplicate')
    """
    params = []

    if status:
        query += " AND status = ?"
        params.append(status)

    if group_by == "lang":
        query += " ORDER BY wiki_lang ASC, sort_key ASC, title ASC"
    elif group_by == "date":
        query += " ORDER BY first_edited_at ASC"
    else:
        query += " ORDER BY sort_key ASC, title ASC"

    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def generate_latex(articles: list[dict], group_by: str, doc_title: str, doc_author: str) -> str:
    preamble = PREAMBLE.replace("__DOC_TITLE__", latex_escape(doc_title))
    preamble = preamble.replace("__DOC_AUTHOR__", latex_escape(doc_author))
    preamble = preamble.replace("__HEADER_TITLE__", latex_escape(doc_title))

    lines = [preamble]
    current_letter = None
    current_lang = None

    for article in articles:
        title = article.get("title") or ""
        lang = article.get("wiki_lang") or "?"
        text_edited = article.get("text_edited") or ""
        text = article.get("text") or ""
        extra_latex = article.get("extra_latex") or ""
        sort_key = article.get("sort_key") or ""

        if not title.strip() or (not text_edited.strip() and not text.strip()):
            continue

        if group_by == "lang" and lang != current_lang:
            current_lang = lang
            current_letter = None
            lines.append(f"\\langsection{{{latex_escape(lang_display(lang))}}}\n")

        if group_by in ("alpha", "lang"):
            letter = first_letter(sort_key)
            if letter != current_letter:
                current_letter = letter
                section_label = letter if letter != r"\#" else "0--9"
                lines.append(f"\\lettersection{{{section_label}}}\n")

        escaped_title = latex_escape(title)
        if text_edited.strip():
            escaped_text = latex_escape(normalize_text(text_edited, cut=False))
        else:
            escaped_text = latex_escape(normalize_text(text, cut=True))

        lines.append(f"\\entry{{{escaped_title}}}{{{lang}}}{{{escaped_text}}}\n")

        if extra_latex.strip():
            lines.append(extra_latex.strip() + "\n")

    lines.append(POSTAMBLE)
    return "\n".join(lines)


def run(db_path: str, out_path: str, group_by: str, status: str | None,
        limit: int | None, doc_title: str, doc_author: str) -> None:
    print(f"Loading articles from '{db_path}' …")
    articles = load_articles(db_path, group_by, status, limit)
    print(f"  {len(articles)} articles loaded.")

    if not articles:
        print("No articles found - aborting.")
        return

    print("Generating LaTeX …")
    latex = generate_latex(articles, group_by, doc_title, doc_author)

    Path(out_path).write_text(latex, encoding="utf-8")
    print(f"  Written: {out_path}")
    print(f"  Compile: xelatex {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generates a LaTeX book from the wiki_contributions SQLite database"
    )
    parser.add_argument("--db", required=True, help="Path to the SQLite database file")
    parser.add_argument("--out", default="my_book.tex", help="Output file")
    parser.add_argument("--group-by", choices=["alpha", "lang", "date"], default="alpha",
                         help="'alpha': one alphabet across all wikis (default); "
                              "'lang': one chapter per language, alphabetical within it; "
                              "'date': chronological by the user's first edit")
    parser.add_argument("--status", default=None, help="Only export articles with this status")
    parser.add_argument("--limit", type=int, default=None, help="Only the first N articles (for testing)")
    parser.add_argument("--title", default="My Wikipedia Articles", help="Book title")
    parser.add_argument("--author", default="", help="Author/name for the title page")
    args = parser.parse_args()

    if not Path(args.db).exists():
        parser.error(f"Database not found: {args.db}")

    run(
        db_path=args.db,
        out_path=args.out,
        group_by=args.group_by,
        status=args.status,
        limit=args.limit,
        doc_title=args.title,
        doc_author=args.author,
    )


if __name__ == "__main__":
    main()
