"""키워드 변형 후보 제안.

- 키워드 자체와 띄어쓰기·하이픈 변형은 기본 체크.
- 끝의 s 를 뗀 단수형(blokees → blokee)은 후보로만 보여 주고 체크하지 않음(adidas → adida 같은 억지가 많아서).
- 뒤에 s 를 붙이는 복수형(kbeauty → kbeautys)은 아예 제안하지 않음.
"""
import re


def suggest(keyword: str):
    """→ [(변형, 기본 체크 여부)]"""
    k = re.sub(r"\s+", " ", keyword.strip().lower())
    if not k:
        return []
    out = [(k, True)]

    def add(v, checked):
        v = v.strip()
        if len(v) >= 3 and v not in [t for t, _ in out]:
            out.append((v, checked))

    for v in [k]:
        if " " in v or "-" in v:
            add(v.replace("-", " "), True)
            add(v.replace(" ", "").replace("-", ""), True)
    if k.endswith("ies"):
        add(k[:-3] + "y", False)
    elif re.search(r"(x|ch|sh|ss)es$", k):
        add(k[:-2], False)
    elif k.endswith("s") and not k.endswith("ss"):
        add(k[:-1], False)
    return out
