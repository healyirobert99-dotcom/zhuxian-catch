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
    index_path = render_report_index()
    print(output)
    print(index_path)


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
    archive_select = build_report_selector(report_date)
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
    .select-wrap {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--line-strong);
      background: var(--paper);
      border-radius: 6px;
      padding: 7px 10px;
      color: var(--muted);
      font-size: 13px;
    }}
    .select-wrap select {{
      appearance: none;
      border: 0;
      background: transparent;
      color: var(--ink);
      font: inherit;
      font-size: 13px;
      min-width: 160px;
      cursor: pointer;
    }}
    .select-wrap select:focus {{ outline: 2px solid var(--accent-soft); outline-offset: 2px; }}
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
          <div class="kicker">V0.4 · {html.escape(report_date)}</div>
          <h1>{html.escape(title)}</h1>
          <p class="subtitle">主线识别与交易复核闭环系统。用于观察市场环境、行业生命周期、ETF/中军载体和操作复核框架。</p>
          <div class="toolbar">
            {archive_select}
            <a class="button" href="./index.html">日报归档</a>
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
        <div class="footer">生成来源：{source_link}。本页面为本地主线交易复核视图，结论依赖当日增量数据与本地缓存完整性。</div>
      </div>
    </main>
  </div>
</body>
</html>
"""


def render_report_index() -> Path:
    reports = discover_reports()
    index_path = REPORT_DIR / "index.html"
    rows = "\n".join(
        f"""
        <tr>
          <td><a href="./{html.escape(item['html_name'])}">{html.escape(item['date'])}</a></td>
          <td>{html.escape(item['title'])}</td>
          <td><a href="./{html.escape(item['md_name'])}">Markdown</a></td>
        </tr>
        """
        for item in reports
    )
    latest_link = f"./{html.escape(reports[0]['html_name'])}" if reports else "#"
    latest_text = html.escape(reports[0]["date"]) if reports else "暂无日报"
    index_path.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>A 股主线研究日报归档</title>
  <style>
    :root {{
      --bg: #f6f5f1;
      --paper: #ffffff;
      --ink: #20231f;
      --muted: #697066;
      --line: #dedbd2;
      --accent: #1d5c56;
      --shadow: 0 14px 36px rgba(29, 31, 27, 0.07);
      --radius: 8px;
      --font: "IBM Plex Sans", "Source Han Sans SC", "Noto Sans CJK SC", "PingFang SC", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); font-family: var(--font); line-height: 1.58; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 36px 24px 56px; }}
    header {{ border-bottom: 1px solid var(--line); padding-bottom: 22px; margin-bottom: 24px; }}
    .kicker {{ color: var(--muted); font-size: 13px; font-weight: 650; letter-spacing: .06em; text-transform: uppercase; }}
    h1 {{ margin: 8px 0 10px; font-size: clamp(32px, 4vw, 52px); line-height: 1.05; }}
    p {{ color: var(--muted); margin: 8px 0; }}
    .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 18px; }}
    .button {{
      border: 1px solid #c8c3b7;
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
    .panel {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 12px 14px; border-bottom: 1px solid var(--line); text-align: left; font-size: 14px; }}
    th {{ background: #f7f6f2; color: #4b5048; font-size: 12px; }}
    tr:last-child td {{ border-bottom: 0; }}
    a {{ color: var(--accent); text-decoration: none; font-weight: 650; }}
    a:hover {{ text-decoration: underline; }}
    .empty {{ padding: 18px; color: var(--muted); }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="kicker">Daily Review Archive</div>
      <h1>A 股主线研究日报归档</h1>
      <p>这里汇总已经生成的 HTML 日报，方便随时回看历史判断和生命周期变化。</p>
      <div class="toolbar">
        <a class="button" href="{latest_link}">打开最新日报：{latest_text}</a>
      </div>
    </header>
    <section class="panel">
      {"<div class='empty'>暂无日报。</div>" if not reports else f"<table><thead><tr><th>日期</th><th>标题</th><th>原文</th></tr></thead><tbody>{rows}</tbody></table>"}
    </section>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )
    return index_path


def build_report_selector(current_date: str) -> str:
    reports = discover_reports()
    if not reports:
        return ""
    options = []
    for item in reports:
        selected = " selected" if item["date"] == current_date else ""
        label = html.escape(item["date"])
        options.append(f'<option value="./{html.escape(item["html_name"])}"{selected}>{label}</option>')
    return (
        '<label class="select-wrap">历史日报 '
        '<select aria-label="选择历史日报" onchange="if (this.value) window.location.href=this.value">'
        + "".join(options)
        + "</select></label>"
    )


def discover_reports() -> list[dict[str, str]]:
    items = []
    for md_path in sorted(REPORT_DIR.glob("a_share_daily_review_*.md"), reverse=True):
        if md_path.stem.endswith("_lifecycle"):
            continue
        match = re.search(r"(\d{4}-\d{2}-\d{2})", md_path.stem)
        if not match:
            continue
        html_path = md_path.with_suffix(".html")
        title = extract_title(md_path.read_text(encoding="utf-8"))
        items.append(
            {
                "date": match.group(1),
                "title": title,
                "md_name": md_path.name,
                "html_name": html_path.name,
            }
        )
    return items


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
