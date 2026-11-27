"""Exolyt 내보내기(xlsx) → 틱톡 탭(해시태그 월별 신규 조회수·영상 수).
시트: '월별 신규 조회수', '월별 신규 영상수', '월별 실측 일수', '해시태그'.
첫 열 키워드, 4번째 열부터 첫 달(헤더 첫 값, 예: 24.12) 기준 연속 월.
잰 일수로 한 달치 환산, 7일 미만인 달은 뺌.

사용: python scripts/import_exolyt.py <파일.xlsx> [서버 주소]
"""
import calendar
import sys
import time

import openpyxl
import requests

F = sys.argv[1]
B = sys.argv[2] if len(sys.argv) > 2 else "https://trend-samurai-production.up.railway.app"
MIN_DAYS = 7

wb = openpyxl.load_workbook(F, read_only=True)


def sheet(name):
    rows = list(wb[name].iter_rows(values_only=True))
    return rows[0], {str(r[0]).strip().lower(): r for r in rows[1:] if r[0]}


head, views = sheet("월별 신규 조회수")
_, vids = sheet("월별 신규 영상수")
_, days = sheet("월별 실측 일수")
tags = {}
if "해시태그" in wb.sheetnames:
    tags = {str(r[0]).strip().lower(): r[-1] for r in list(wb["해시태그"].iter_rows(values_only=True))[1:] if r[0]}

first = str(head[3]).rstrip("*")              # 예: '24.12'
y, m = 2000 + int(first.split(".")[0]), int(first.split(".")[1])
months = []
for _ in range(len(head) - 3):
    months.append(f"{y}-{m:02d}")
    m += 1
    if m > 12:
        y, m = y + 1, 1

ok = miss = 0
for k, row in views.items():
    nrow = vids.get(k) or [None] * len(head)
    drow = days.get(k) or [None] * len(head)
    V, N = [], []
    for i, mo in enumerate(months):
        d = drow[3 + i]
        if not d or d < MIN_DAYS:
            continue
        yy, mm = map(int, mo.split("-"))
        f = calendar.monthrange(yy, mm)[1] / d
        if row[3 + i] is not None:
            V.append([mo + "-01", round(row[3 + i] * f)])
        if nrow[3 + i] is not None:
            N.append([mo + "-01", round(nrow[3 + i] * f)])
    if len(V) < 2:
        continue
    body = {"keyword": k, "rows": V, "unit": "exolyt", "label": tags.get(k) or "#" + k.replace(" ", ""), "videos": N}
    for t in range(5):
        try:
            r = requests.post(B + "/api/tiktok/history", json=body, timeout=120)
            if r.status_code == 404:
                miss += 1
                print("키워드 없음:", k)
                break
            r.raise_for_status()
            ok += 1
            break
        except Exception as e:
            print("재시도", k, e)
            time.sleep(10)
print(f"넣음 {ok} · 키워드 없음 {miss}")
