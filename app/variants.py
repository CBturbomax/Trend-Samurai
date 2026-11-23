"""키워드 변형 후보 제안. 레딧 검색은 단어 단위라 단수/복수를 따로 찾아야 함."""
import re


def suggest(keyword: str):
    k = re.sub(r"\s+", " ", keyword.strip().lower())
    if not k:
        return []
    out = [k]

    def add(v):
        v = v.strip()
        if len(v) >= 3 and v not in out:
            out.append(v)

    # 단수 ↔ 복수 (마지막 단어 기준)
    if k.endswith("ies"):
        add(k[:-3] + "y")
    if re.search(r"(x|ch|sh|ss)es$", k):
        add(k[:-2])
    if k.endswith("s") and not k.endswith("ss"):
        add(k[:-1])
    else:
        add(k + "s")
    # 띄어쓰기 · 하이픈
    for v in list(out):
        if " " in v or "-" in v:
            add(v.replace("-", " "))
            add(v.replace(" ", "").replace("-", ""))
            add(v.replace(" ", "-"))
    return out
