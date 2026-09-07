"""词典查询模块：通过有道词典 jsonapi 查询单词的音标、词性、中文释义、英文例句、中文翻译。"""
import json
import urllib.request
import urllib.parse
import urllib.error
import re

YOUDAO_URL = "https://dict.youdao.com/jsonapi"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://dict.youdao.com",
    "Referer": "https://dict.youdao.com/",
}


def _strip_html(s):
    """去除例句中的 <b> 等 HTML 标记。"""
    return re.sub(r"<[^>]+>", "", s or "").strip()


# ── 记忆提示相关 ──────────────────────────────────────────────

# 词源句子信号权重：越有助记价值权重越高。
# 注意：同一语义族的信号只计一次（用 group 标识），避免"源自…源于…"这类
# 同义反复把分数顶上去，反而压过真正有信息量的句子。
_SIGNAL_RULES = [
    ("词根词缀", 100, "root"),
    ("合成", 60, "compound"),
    ("词源同", 50, "cognate"),
    ("同源", 50, "cognate"),
    ("词根", 20, "root"),
    ("来自", 30, "origin"),
    ("源于", 30, "origin"),
    ("源自", 30, "origin"),
    ("来源于", 30, "origin"),
    ("来源", 25, "origin"),
    ("拉丁语", 10, "lang"),
    ("希腊语", 10, "lang"),
    ("古英语", 10, "lang"),
    ("古法语", 10, "lang"),
]
# "语言名 + 紧邻的具体词形"是强信号：古英语stæf 比泛泛的"源于拉丁语"好记得多
_LANG_FORM_RE = re.compile(
    r"(?:拉丁语|希腊语|古英语|古法语|法语|古挪威语|日耳曼语|印欧语)"
    r"[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ\-]*"
)
_PENALTY_RULES = [
    ("既可以", -20),
    ("兼有", -20),
    ("两种词性", -20),
]


def _score_sentence(s):
    """给词源句子打分：信号总量 ÷ 长度因子 = 信息密度。

    除以长度因子是关键。词源原文常是上百字的历史考据，信号堆得多但截断后
    只剩半句话；归一化后，短而密的句子（如"源于古英语stæf（手杖）"）才能胜出。
    """
    best_by_group = {}
    for kw, val, grp in _SIGNAL_RULES:
        if kw in s:
            best_by_group[grp] = max(best_by_group.get(grp, 0), val)

    score = sum(best_by_group.values())
    if _LANG_FORM_RE.search(s):          # 语言名 + 具体词形，额外加分
        score += 45
    for kw, val in _PENALTY_RULES:
        if kw in s:
            score += val
    if len(s) < 12:                      # 太短没信息量
        score -= 30
    if score <= 0:
        return 0.0

    # 密度归一化：50 字为基准，越长单位价值越低
    return score / ((len(s) / 50.0) ** 0.5)


def _split_sentences(text):
    """按句末标点切句，保留标点。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[。！？!?])", text)
    return [p.strip() for p in parts if p.strip()]


# 句首过渡语：词源原文是连贯段落，切句后常残留"再往前看，"这类连接词，
# 单独成句时读着突兀，去掉它们能让提示更干净
_LEADIN_RE = re.compile(
    r"^(?:再往前看|往前追溯|追溯|此外|同时|另外|其实|事实上|并且|而且"
    r"|不过|然而|因此|所以|而后|后来|随后|接着)[，,、]\s*"
)


def _smart_trim(text, maxlen=48):
    """优先在句内标点处断句，避免切出半截词；超长加省略号。"""
    text = re.sub(r"\s+", " ", text or "").strip()
    # 反复剥离句首过渡语（可能有连续多个）
    for _ in range(3):
        new = _LEADIN_RE.sub("", text)
        if new == text:
            break
        text = new
    if len(text) <= maxlen:
        return text
    cut = text[:maxlen]
    for sep in ("；", "，", ",", "、"):
        idx = cut.rfind(sep)
        if idx >= maxlen * 0.5:          # 断点不能太靠前，否则信息全丢
            return cut[:idx].rstrip("，,；;、") + "…"
    return cut + "…"


def _pick_root_hint(etyms_zh):
    """从中文词源里挑出最有记忆价值的那一句。

    词源文本常常是好几百字的历史考据，直接贴进钉钉卡片会喧宾夺主，
    所以只挑一句信息密度最高的（优先"词根词缀""合成词""词源同 X"）。

    返回 (label, text)：label 为 词根 / 合成 / 词源，无素材时返回 ("", "")。
    """
    if not etyms_zh:
        return "", ""

    # 先看有没有现成的"词根词缀："拆解 —— 最精炼，直接用
    for item in etyms_zh:
        v = (item.get("value") or "").strip()
        if v.startswith("词根词缀"):
            v = re.sub(r"^词根词缀[:：]\s*", "", v)
            return "词根", _smart_trim(v, 60)

    # 否则把所有词源条目拆成句子，选信息密度最高的一句
    best, best_score = "", 0.0
    for item in etyms_zh:
        for sent in _split_sentences(item.get("value")):
            sc = _score_sentence(sent)
            if sc > best_score:
                best, best_score = sent, sc
    if best_score <= 0:
        return "", ""

    # 按内容挑标签：合成词叫"词根"会误导
    label = "合成" if "合成" in best else "词源"
    return label, _smart_trim(best, 52)


def _collect_words(items, limit, word, key):
    """从 rel_word / syno 结构里收集干净的词形列表。"""
    out, seen = [], set()
    low_self = (word or "").lower()
    for bucket in items:
        for w in bucket:
            c = (w.get(key) or "").strip()
            if not c or " " in c:              # 过滤 "whoop it up" 这类短语
                continue
            lc = c.lower()
            if lc == low_self or lc in seen:    # 排除自身与重复
                continue
            seen.add(lc)
            out.append(c)
            if len(out) >= limit:
                return out
    return out


def _extract_hints(payload, word):
    """提取记忆提示素材，三层按可用性独立填充，互不阻塞。

    返回 dict，键可能包含 root / cognates / synonyms；一个都没有则返回 None。
    """
    hints = {}

    # 第 1 层：词根词缀拆解 / 合成词说明 / 词源故事
    etyms = ((payload.get("etym") or {}).get("etyms") or {})
    label, root = _pick_root_hint(etyms.get("zh") or [])
    if root:
        hints["root"] = root
        hints["root_label"] = label

    # 第 2 层：同根词（rel_word）
    rels = (payload.get("rel_word") or {}).get("rels") or []
    buckets = [(r.get("rel", {}).get("words") or []) for r in rels]
    cognates = _collect_words(buckets, 3, word, "word")
    if cognates:
        hints["cognates"] = cognates

    # 第 3 层：近义词（syno）
    synos = (payload.get("syno") or {}).get("synos") or []
    sbuckets = [(s.get("syno", {}).get("ws") or []) for s in synos]
    # 近义词只取 3 个：有道返回的靠后项常是 demission / segregant 这类生僻词，
    # 列太多反而增加记忆负担
    synonyms = _collect_words(sbuckets, 3, word, "w")
    if synonyms:
        hints["synonyms"] = synonyms

    return hints or None


def fetch_entry(word):
    """查询单词的完整释义。返回 dict 或 None（查询失败时）。"""
    data = urllib.parse.urlencode({"q": word, "le": "eng"}).encode()
    req = urllib.request.Request(YOUDAO_URL, data=data, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception):
        return None

    ec = payload.get("ec", {}).get("word", [])
    if not ec:
        return None

    w = ec[0]
    usphone = (w.get("usphone") or w.get("phonetic") or "").strip()
    ukphone = (w.get("ukphone") or "").strip()

    # 词性 + 中文释义: trs[*].tr[*].l.i[*]
    senses = []  # [(pos, def_zh)]
    for t in w.get("trs", [])[:3]:
        for inner in t.get("tr", []):
            l = inner.get("l", {})
            i_list = l.get("i", [])
            if i_list:
                full = i_list[0]
                # 提取词性: "v. 抛弃..." -> pos="v.", def="抛弃..."
                m = re.match(r"^([nvij]\.?|adj\.|adv\.|prep\.|conj\.|pron\.|num\.|art\.|aux\.|interj\.|int\.|abbr\.|n\.|v\.|vi\.|vt\.|n\.v\.|)\s*(.*)$", full)
                if m and m.group(1):
                    pos = m.group(1).strip()
                    definition = m.group(2).strip()
                else:
                    pos = ""
                    definition = full.strip()
                # 截短过长释义
                if len(definition) > 120:
                    definition = definition[:117] + "..."
                senses.append((pos, definition))

    # 英文例句 + 中文翻译: blng_sents_part.sentence-pair[]
    bp = payload.get("blng_sents_part", {}) or {}
    pairs = bp.get("sentence-pair", []) or []
    example_en, example_zh = "", ""
    for p in pairs:
        en = _strip_html(p.get("sentence-eng") or p.get("sentence") or "")
        zh = _strip_html(p.get("sentence-translation") or "")
        if en and zh:
            example_en, example_zh = en, zh
            break

    if not senses and not example_en:
        return None

    return {
        "word": word,
        "usphone": usphone,
        "ukphone": ukphone,
        "senses": senses[:2],  # 最多两条释义
        "example_en": example_en[:200],
        "example_zh": example_zh[:140],
        "hints": _extract_hints(payload, word) or {},
    }