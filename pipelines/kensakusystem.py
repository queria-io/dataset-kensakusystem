"""会議録検索システム（kensakusystem.jp）から、議会の会議録を発言ごとに取得する。

閲覧画面のツリー（See.exe に treedepth を POST する）を年 → 会議の順にたどり、会議の日ごとの
会議録を取る。1 日分の会議録は、発言の一覧（r_Speakers.exe）で発言の位置を取り、
ダウンロード（GetPerson.exe）にすべての位置を渡して、発言を並べたテキストで受け取る。

出力: data/minutes/<地方公共団体コード>/<西暦年>.ndjson.gz（1 行 = 1 発言）

ツリーの年は和暦（令和・平成）。取得するのは新しい方から KENSAKU_YEARS 年分（0 ならすべての年）。
直近 2 年より前の年は、ファイルが既にあれば飛ばす（手元で全年を取る途中で止まったときに続きから取るため）。
"""

import gzip
import json
import logging
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request

from shared.fetch import replacing, request

logger = logging.getLogger("pipelines")

HOST = "https://www.kensakusystem.jp"
USER_AGENT = "dataset-kensakusystem (+https://github.com/queria-io/dataset-kensakusystem)"
OUTPUT = Path("data/minutes")
YEARS = int(os.environ.get("KENSAKU_YEARS", "2"))
INTERVAL_SECONDS = 1.0
ERAS = {"令和": 2018, "平成": 1988, "昭和": 1925}


@dataclass(frozen=True)
class Assembly:
    lg_code: str
    name: str
    path: str
    code: str


ASSEMBLIES = [
    Assembly("131105", "目黒区議会", "meguro", "hxcvax8v0tgrybompl"),
    Assembly("131164", "豊島区議会", "toshima", "jejucn3ptc8dzc6egg"),
    Assembly("131229", "葛飾区議会", "katsushika", "n9goyxzrwrkcenlj8f"),
]


def get(url: str, data: list[tuple[str, str]] | None = None) -> str:
    body = urlencode(data, encoding="cp932").encode() if data is not None else None
    time.sleep(INTERVAL_SECONDS)
    return request(Request(url, body, headers={"User-Agent": USER_AGENT}), lambda r: r.read()).decode("cp932", "replace")


def western_year(label: str) -> int:
    """「令和 5年」「平成元年」を西暦にする。"""
    m = re.match(r"(令和|平成|昭和)\s*(元|\d+)年", label)
    if not m:
        raise ValueError(f"和暦の年が読めない: {label!r}")
    return ERAS[m[1]] + (1 if m[2] == "元" else int(m[2]))


def parse_date(text: str) -> date:
    m = re.search(r"開催日：\s*(令和|平成|昭和)\s*(元|\d+)年\s*(\d+)月\s*(\d+)日", text)
    if not m:
        raise ValueError("開催日が読めない")
    return date(western_year(f"{m[1]}{m[2]}年"), int(m[3]), int(m[4]))


def tree_values(html: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"treedepth\.value='([^']+)'", html)))


def is_session(value: str) -> bool:
    """「令和 5年 第1回定例会 」のように、年のあとに会議名が続く値。"""
    return bool(re.match(r"(令和|平成|昭和)\s*(元|\d+)年\s+\S", value))


def sessions(a: Assembly) -> list[str]:
    """ツリーの年をすべて開き、会議（定例会・委員会）の treedepth を集める。"""
    see = f"{HOST}/{a.path}/cgi-bin3/See.exe"
    seen: set[str] = set()
    found: list[str] = []
    queue = tree_values(get(f"{see}?Code={a.code}"))
    while queue:
        value = queue.pop(0)
        if value in seen:
            continue
        seen.add(value)
        if is_session(value):
            found.append(value)
            continue
        queue += [v for v in tree_values(get(see, [("Code", a.code), ("treedepth", value)])) if v not in seen]
    return found


def documents(a: Assembly, session: str) -> list[str]:
    """会議の日ごとの会議録のファイル名。"""
    html = get(f"{HOST}/{a.path}/cgi-bin3/See.exe", [("Code", a.code), ("treedepth", session)])
    return list(dict.fromkeys(re.findall(r"ResultFrame\.exe\?Code=\w+&fileName=(\w+)&", html)))


def speeches(a: Assembly, session: str, file_name: str) -> Iterator[dict]:
    base = f"{HOST}/{a.path}/cgi-bin3"
    frame = get(f"{base}/ResultFrame.exe?Code={a.code}&fileName={file_name}&startPos=0")
    speakers_src = re.search(r'SRC="([^"]*r_Speakers\.exe[^"]*)"', frame, re.I)[1]
    listing = get(HOST + speakers_src)
    positions = re.findall(r'name="downloadPos" value="(\d+)"', listing)
    # ページ（P.1 など）へのリンクを除き、発言へのリンクだけを読む
    names = [n.strip() for n in re.findall(r'r_TextFrame\.exe[^"]*" TARGET="TEXTW"><font class="TEXT5">([^<]+)<', listing)]
    text = get(f"{base}/GetPerson.exe", [("Code", a.code), ("fileName", file_name)] + [("downloadPos", p) for p in positions])
    header, _, body = text.replace("\r\n", "\n").partition("\n\n")
    meeting = re.search(r"会議名：(.+)", header)
    entries = re.split(r"^○", body, flags=re.M)[1:]
    if not (len(entries) == len(positions) == len(names)):
        raise SystemExit(f"{a.name} {file_name}: 発言の数が合わない（本文 {len(entries)}、位置 {len(positions)}、名前 {len(names)}）")
    held = parse_date(header)
    for order, (entry, position, name) in enumerate(zip(entries, positions, names)):
        speech = entry.removeprefix(name).strip("　 \n")
        yield {
            "lg_code": a.lg_code,
            "assembly": a.name,
            "file_name": file_name,
            "session": session.strip(),
            "meeting_name": meeting[1].strip() if meeting else session.strip(),
            "date": held.isoformat(),
            "speech_order": order,
            "speaker": name,
            "speech": speech,
            "speech_url": f"{base}/ResultFrame.exe?Code={a.code}&fileName={file_name}&startPos={position}",
        }


def years_to_fetch(years: list[int], keep: int) -> list[int]:
    """新しい方から keep 年分（0 ならすべて）。"""
    ordered = sorted(set(years), reverse=True)
    return ordered if keep == 0 else ordered[:keep]


def download_assembly(a: Assembly, output: Path, fetched_at: str) -> int:
    by_year: dict[int, list[str]] = {}
    for s in sessions(a):
        by_year.setdefault(western_year(s), []).append(s)
    targets = set(years_to_fetch(list(by_year), YEARS))
    # 直近 2 年は会議録が後から足されるので、ファイルがあっても取り直す
    latest = set(years_to_fetch(list(by_year), 2))
    total = 0
    for year in sorted(targets, reverse=True):
        path = output / a.lg_code / f"{year}.ndjson.gz"
        if year not in latest and path.exists():
            continue
        rows = [row for s in by_year[year] for f in documents(a, s) for row in speeches(a, s, f)]
        write(path, rows, fetched_at)
        logger.info("  %s %d年: %d 発言", a.name, year, len(rows))
        total += len(rows)
    return total


def write(path: Path, rows: list[dict], fetched_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with replacing(path) as partial, gzip.open(partial, "wt", encoding="utf-8") as out:
        out.writelines(json.dumps({**row, "fetched_at": fetched_at}, ensure_ascii=False) + "\n" for row in rows)


def check_assembly(a: Assembly, output: Path, fetched_at: str) -> int:
    """PR のビルド確認用。ツリーの先頭にある年（最新の年）の、先頭の会議の 1 日分だけを取る。"""
    session = sessions(a)[0]
    file_name = documents(a, session)[0]
    rows = list(speeches(a, session, file_name))
    write(output / a.lg_code / f"{western_year(session)}.ndjson.gz", rows, fetched_at)
    logger.info("  %s %s %s: %d 発言", a.name, session.strip(), file_name, len(rows))
    return len(rows)


def download_kensakusystem(output: Path = OUTPUT) -> int:
    fetched_at = datetime.now(UTC).isoformat()
    # FDL_WORKING が無いのは PR のビルド確認。公開しないので各議会の最新の年の 1 日分だけを取る
    run = download_assembly if os.environ.get("FDL_WORKING") else check_assembly
    total = sum(run(a, output, fetched_at) for a in ASSEMBLIES)
    if total == 0:
        raise SystemExit("1 件も取得できない（会議録検索システムの形が変わった？）")
    return total
