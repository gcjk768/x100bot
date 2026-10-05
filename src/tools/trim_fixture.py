"""Trim a saved Fuji X Weekly page to the parts the parser reads, so the public repo carries no article prose
or photos: og:title, the byline, each settings paragraph with the caption line after it, and index links."""
import sys
from pathlib import Path

from bs4 import BeautifulSoup


def trim(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", property="og:title")
    author = soup.select_one("[rel=author], .byline .author a, .author a")
    content = soup.select_one("div.entry-content")
    keep = []
    for p in content.find_all("p"):
        flat = p.get_text(" ", strip=True)
        if flat.startswith("Film Simulation:") or ("Dynamic Range:" in flat and "White Balance:" in flat):
            keep.append(str(p))
            nxt = p.find_next_sibling("p")
            if nxt and "using this" in nxt.get_text():
                keep.append(f"<p>{nxt.get_text(' ', strip=True)}</p>")
    if not keep:   # an index page: only the links
        keep = [f'<a href="{a["href"]}">{a.get_text(" ", strip=True)}</a>' for a in content.select("a[href]")]
    return ("<html><head>" + (str(og) if og else "") + "</head><body>"
            + (f'<a rel="author">{author.get_text(" ", strip=True)}</a>' if author else "")
            + '<div class="entry-content">\n' + "\n".join(keep) + "\n</div></body></html>\n")


if __name__ == "__main__":
    for f in sys.argv[1:]:
        p = Path(f)
        p.write_text(trim(p.read_text(encoding="utf-8")), encoding="utf-8")
        print(p.name, p.stat().st_size)
