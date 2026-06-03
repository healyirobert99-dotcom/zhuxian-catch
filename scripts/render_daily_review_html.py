from __future__ import annotations

import argparse
import html
import re
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]
REPORT_DIR = BASE / "reports" / "daily_review"


def main() -> None:
    args = parse_args()
    source = resolve_source(args)
    output = Path(args.output).resolve() if args.output else source.with_suffix(".html")
    markdown = source.read_text(encoding="utf-8")
    output.write_text(render_html(markdown, source), encoding="utf-8")
    print(output)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render an A-share daily review Markdown report as HTML.")
    parser.add_argument("--trade-date", help="Trade date such as 20260601 or 2026-06-01.")
    parser.add_argument("--input", help="Markdown report path. Overrides --trade-date.")
    parser.add_argument("--output", help="HTML output path. Defaults to the Markdown path with .html suffix.")
    return parser.parse_args()


def resolve_source(args: argparse.Namespace) -> Path:
    if args.input:
        source = Path(args.input).resolve()
    elif args.trade_date:
        date = normalize_date(args.trade_date)
        source = REPORT_DIR / f"a_share_daily_review_{date}.md"
    else:
        candidates = sorted(REPORT_DIR.glob("a_share_daily_review_*.md"))
        candidates = [p for p in candidates if not p.stem.endswith("_lifecycle")]
        if not candidates:
            raise SystemExit("No daily review Markdown reports found.")
        source = candidates[-1]
    if not source.exists():
        raise SystemExit(f"Markdown report not found: {source}")
    return source


def normalize_date(value: str) -> str:
    text = value.strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    raise SystemExit(f"Invalid trade date: {value}")


def render_html(markdown: str, source: Path) -> str:
    title = extract_title(markdown)
    report_date = extract_report_date(title, source)
    body_md, appendix_md = split_appendix(markdown)
    nav = build_nav(body_md)
    body_html = markdown_to_html(body_md)
    appendix_html = render_appendix(appendix_md) if appendix_md.strip() else ""
    source_link = html.escape(source.name)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f6f5f1;
      --paper: #ffffff;
      --ink: #20231f;
      --muted: #697066;
      --line: #dedbd2;
      --line-strong: #c8c3b7;
      --accent: #1d5c56;
      --accent-soft: #e0ece8;
      --risk: #9f3f32;
      --risk-soft: #f2e1dc;
      --warn: #9a6a16;
      --warn-soft: #f3ead2;
      --good: #2d6e45;
      --good-soft: #e1efe4;
      --shadow: 0 14px 36px rgba(29, 31, 27, 0.07);
      --radius: 8px;
      --font: "IBM Plex Sans", "Source Han Sans SC", "Noto Sans CJK SC", "PingFang SC", sans-serif;
      --mono: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: var(--bg);
      font-family: var(--font);
      line-height: 1.58;
      letter-spacing: 0;
    }}
    a {{ color: inherit; }}
    .shell {{ display: grid; grid-template-columns: 236px minmax(0, 1fr); min-height: 100vh; }}
    aside {{
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 24px 18px;
      border-right: 1px solid var(--line);
      background: #eeece5;
      overflow: auto;
    }}
    .brand {{ display: grid; gap: 8px; margin-bottom: 28px; }}
    .brand small {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .08em; }}
    .brand strong {{ font-size: 19px; line-height: 1.25; font-weight: 740; }}
    nav {{ display: grid; gap: 4px; }}
    nav a {{ display: block; padding: 9px 10px; border-radius: 6px; color: var(--muted); text-decoration: none; font-size: 14px; }}
    nav a:hover, nav a:focus {{ color: var(--ink); background: rgba(255,255,255,.72); outline: none; }}
    main {{ padding: 32px; }}
    .page {{ max-width: 1320px; margin: 0 auto; }}
    header {{ padding: 28px 0 22px; border-bottom: 1px solid var(--line); margin-bottom: 24px; }}
    .kicker {{ color: var(--muted); font-size: 13px; font-weight: 650; letter-spacing: .06em; text-transform: uppercase; }}
    h1 {{ margin: 8px 0 10px; font-size: clamp(30px, 4vw, 52px); line-height: 1.05; font-weight: 760; }}
    .subtitle {{ max-width: 860px; margin: 0; color: var(--muted); font-size: 16px; }}
    .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }}
    .button {{
      appearance: none;
      border: 1px solid var(--line-strong);
      background: var(--paper);
      color: var(--ink);
      border-radius: 6px;
      padding: 9px 12px;
      font: inherit;
      font-size: 13px;
      cursor: pointer;
      text-decoration: none;
    }}
    .button:hover {{ border-color: var(--accent); color: var(--accent); }}
    article > section {{
      margin: 28px 0;
      padding: 18px;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      scroll-margin-top: 18px;
    }}
    article > section:first-child {{ margin-top: 0; }}
    h2 {{ margin: 0 0 14px; font-size: 22px; line-height: 1.25; font-weight: 730; }}
    h3 {{ margin: 22px 0 10px; font-size: 17px; line-height: 1.3; }}
    p {{ margin: 10px 0; }}
    blockquote {{
      margin: 14px 0;
      border-left: 4px solid var(--accent);
      padding: 12px 15px;
      background: var(--accent-soft);
      border-radius: 0 var(--radius) var(--radius) 0;
      color: #27423f;
    }}
    ul, ol {{ padding-left: 22px; }}
    li {{ margin: 7px 0; }}
    code {{
      font-family: var(--mono);
      background: #ebe8df;
      border: 1px solid var(--line);
      border-radius: 4px;
      padding: 1px 5px;
      font-size: .92em;
    }}
    pre {{
      overflow: auto;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: #f7f6f2;
    }}
    pre code {{ padding: 0; border: 0; background: transparent; }}
    .table-wrap {{
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--paper);
      margin: 14px 0;
    }}
    table {{ width: 100%; border-collapse: collapse; min-width: 780px; }}
    th, td {{
      padding: 10px 11px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }}
    th {{
      position: sticky;
      top: 0;
      z-index: 1;
      background: #f7f6f2;
      color: #4b5048;
      font-size: 12px;
      font-weight: 760;
    }}
    td.num, th.num {{ text-align: right; font-family: var(--mono); white-space: nowrap; }}
    tr:last-child td {{ border-bottom: 0; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .badge-a {{ color: var(--good); background: var(--good-soft); border: 1px solid #bdd9c4; }}
    .badge-b {{ color: var(--accent); background: var(--accent-soft); border: 1px solid #c7ddd7; }}
    .badge-c {{ color: var(--warn); background: var(--warn-soft); border: 1px solid #dfcf9f; }}
    .badge-risk {{ color: var(--risk); background: var(--risk-soft); border: 1px solid #ddb8af; }}
    .badge-neutral {{ color: #555b53; background: #ebe8df; border: 1px solid #d8d2c6; }}
    details {{
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--paper);
      box-shadow: var(--shadow);
      margin: 12px 0;
      overflow: hidden;
    }}
    summary {{ cursor: pointer; padding: 15px 18px; font-weight: 720; background: #f7f6f2; }}
    details[open] summary {{ border-bottom: 1px solid var(--line); }}
    .details-body {{ padding: 16px 18px 18px; }}
    .footer {{ margin: 36px 0 12px; padding-top: 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: 13px; }}
    @media (max-width: 960px) {{
      .shell {{ grid-template-columns: 1fr; }}
      aside {{ position: static; height: auto; border-right: 0; border-bottom: 1px solid var(--line); }}
      nav {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      main {{ padding: 20px; }}
    }}
    @media print {{
      body {{ background: white; }}
      aside, .toolbar {{ display: none; }}
      .shell {{ display: block; }}
      main {{ padding: 0; }}
      article > section, .table-wrap, details {{ box-shadow: none; }}
      details {{ page-break-inside: avoid; }}
      details:not([open]) .details-body {{ display: block; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="brand">
        <small>Mainline Review</small>
        <strong>A 股主线研究日报</strong>
      </div>
      <nav aria-label="页面目录">
        {nav}
        <a href="#appendix">附录</a>
      </nav>
    </aside>
    <main>
      <div class="page">
        <header>
          <div class="kicker">V0.3 · {html.escape(report_date)}</div>
          <h1>{html.escape(title)}</h1>
          <p class="subtitle">主线识别与研究闭环系统。用于观察市场环境、行业生命周期、主线延续和退潮风险，不提供交易指令。</p>
          <div class="toolbar">
            <a class="button" href="./{source_link}">查看 Markdown 原文</a>
            <button class="button" type="button" onclick="window.print()">打印 / 导出 PDF</button>
          </div>
        </header>
        <article>
          {body_html}
        </article>
        <section id="appendix">
          <h2>附录</h2>
          {appendix_html}
        </section>
        <div class="footer">生成来源：{source_link}。本页面为本地研究报告视图，不构成投资建议。</div>
      </div>
    </main>
  </div>
</body>
</html>
"""


def extract_title(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "A 股主线研究日报"


def extract_report_date(title: str, source: Path) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
    if match:
        return match.group(1)
    match = re.search(r"(\d{4}-\d{2}-\d{2})", source.stem)
    return match.group(1) if match else "未知日期"


def split_appendix(markdown: str) -> tuple[str, str]:
    marker = "\n# 附录"
    if marker not in markdown:
        return markdown, ""
    body, appendix = markdown.split(marker, 1)
    return body, "# 附录" + appendix


def build_nav(markdown: str) -> str:
    links = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            text = line[3:].strip()
            links.append(f'<a href="#{slug(text)}">{html.escape(strip_md(text))}</a>')
    return "\n        ".join(links)


def render_appendix(markdown: str) -> str:
    lines = markdown.splitlines()
    chunks: list[tuple[str, list[str]]] = []
    current_title = "附录"
    current: list[str] = []
    for line in lines:
        if line.startswith("## 附录"):
            if current:
                chunks.append((current_title, current))
            current_title = line[3:].strip()
            current = []
        elif line.startswith("# 附录"):
            continue
        else:
            current.append(line)
    if current:
        chunks.append((current_title, current))
    parts = []
    for index, (title, chunk_lines) in enumerate(chunks):
        open_attr = " open" if index == 0 else ""
        chunk_html = markdown_to_html("\n".join(chunk_lines), sectionize=False)
        parts.append(
            f'<details{open_attr}><summary>{html.escape(title)}</summary>'
            f'<div class="details-body">{chunk_html}</div></details>'
        )
    return "\n".join(parts)


def markdown_to_html(markdown: str, *, sectionize: bool = True) -> str:
    lines = markdown.splitlines()
    parts: list[str] = []
    paragraph: list[str] = []
    list_lines: list[str] = []
    in_code = False
    code_lines: list[str] = []
    section_open = False
    i = 0

    def flush_paragraph() -> None:
        if paragraph:
            text = " ".join(line.strip() for line in paragraph if line.strip())
            parts.append(f"<p>{inline_md(text)}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if not list_lines:
            return
        ordered = all(re.match(r"^\d+\.\s+", line) for line in list_lines)
        tag = "ol" if ordered else "ul"
        items = []
        for line in list_lines:
            item = re.sub(r"^(\d+\.|-)\s+", "", line).strip()
            items.append(f"<li>{inline_md(item)}</li>")
        parts.append(f"<{tag}>" + "".join(items) + f"</{tag}>")
        list_lines.clear()

    def close_section() -> None:
        nonlocal section_open
        if sectionize and section_open:
            parts.append("</section>")
            section_open = False

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush_paragraph()
            flush_list()
            if in_code:
                parts.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines.clear()
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_lines.append(line)
            i += 1
            continue
        if is_table_start(lines, i):
            flush_paragraph()
            flush_list()
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            parts.append(render_table(table_lines))
            continue
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_list()
            i += 1
            continue
        if stripped == "---":
            flush_paragraph()
            flush_list()
            parts.append("<hr>")
            i += 1
            continue
        if line.startswith("## "):
            flush_paragraph()
            flush_list()
            close_section()
            text = line[3:].strip()
            if sectionize:
                parts.append(f'<section id="{slug(text)}"><h2>{inline_md(text)}</h2>')
                section_open = True
            else:
                parts.append(f'<h2 id="{slug(text)}">{inline_md(text)}</h2>')
            i += 1
            continue
        if line.startswith("### "):
            flush_paragraph()
            flush_list()
            text = line[4:].strip()
            parts.append(f'<h3 id="{slug(text)}">{inline_md(text)}</h3>')
            i += 1
            continue
        if line.startswith("# "):
            i += 1
            continue
        if line.startswith(">"):
            flush_paragraph()
            flush_list()
            quote = line.lstrip(">").strip()
            parts.append(f"<blockquote>{inline_md(quote)}</blockquote>")
            i += 1
            continue
        if re.match(r"^(\d+\.|-)\s+", stripped):
            flush_paragraph()
            list_lines.append(stripped)
            i += 1
            continue
        paragraph.append(line)
        i += 1

    flush_paragraph()
    flush_list()
    if in_code:
        parts.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    close_section()
    return "\n".join(parts)


def is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    first = lines[index].strip()
    second = lines[index + 1].strip()
    return first.startswith("|") and second.startswith("|") and bool(re.search(r"\|\s*:?-{3,}:?\s*(\||$)", second))


def render_table(lines: list[str]) -> str:
    header = split_table_row(lines[0])
    align = ["num" if cell.strip().endswith(":") else "" for cell in split_table_row(lines[1])]
    body = [split_table_row(line) for line in lines[2:]]
    out = ['<div class="table-wrap"><table>']
    out.append("<thead><tr>")
    for idx, cell in enumerate(header):
        cls = ' class="num"' if idx < len(align) and align[idx] == "num" else ""
        out.append(f"<th{cls}>{inline_md(cell)}</th>")
    out.append("</tr></thead><tbody>")
    for row in body:
        out.append("<tr>")
        for idx, cell in enumerate(row):
            cls = ' class="num"' if idx < len(align) and align[idx] == "num" else ""
            out.append(f"<td{cls}>{format_cell(cell)}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def split_table_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def format_cell(cell: str) -> str:
    raw = cell.strip()
    if raw in {"A", "B", "C"}:
        return f'<span class="badge badge-{raw.lower()}">{raw}</span>'
    if "退潮" in raw or "风险" in raw:
        return f'<span class="badge badge-risk">{inline_md(raw)}</span>'
    if raw in {"暂无", "NA", "无"}:
        return f'<span class="badge badge-neutral">{html.escape(raw)}</span>'
    return inline_md(raw)


def inline_md(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def strip_md(text: str) -> str:
    return re.sub(r"[*_`#]", "", text)


def slug(text: str) -> str:
    clean = strip_md(text).strip().lower()
    clean = re.sub(r"\s+", "-", clean)
    clean = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff-]+", "", clean)
    return clean or "section"


if __name__ == "__main__":
    main()
