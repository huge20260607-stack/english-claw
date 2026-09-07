"""
English Claw · 每日单词推送（GitHub Actions 版）
每天定时触发 → 判断工作日 → 抽取今日 5 词 → 有道查询 →
拼 Markdown → 按各群配置的时刻分别推送到钉钉。

设计要点：
  · 一个 workflow 管多个群，每个群可配各自的推送时刻（hour/minute）
  · 当日词表只抽取一次，多群共用 —— 天然保证各群当天内容完全一致，
    同时只调用一轮有道 API，省时省配额
  · 仅工作日推送：跳过周末与法定节假日，调休上班日照常发

时区说明：
  GitHub Actions 的 cron 使用 UTC 时间，所有 cron 表达式均按北京时间
  减 8 小时换算后填入。代码内部统一用北京时间判断工作日和推送时刻。
  例如：北京时间 09:00 = UTC 01:00 → cron '0 1 * * *'

环境变量（通过 GitHub Secrets 注入）：
  TARGETS            必填（多群模式）：JSON 数组，每项
                     {"name":群名, "webhook":URL, "secret":加签密钥,
                      "hour":9, "minute":0}
                     例：
                     [{"name":"LSBG-OBD海外业务部","webhook":"https://oapi.dingtalk.com/robot/send?access_token=xxx","secret":"SECxxx","hour":9,"minute":0},
                      {"name":"海外英语能力提升","webhook":"...","secret":"...","hour":11,"minute":30},
                      {"name":"海外设计","webhook":"...","secret":"...","hour":17,"minute":30}]
  WORDS_PER_DAY      选填：每日推送词数（默认 5）
  DIVERSITY_DECAY    选填：主题多样性衰减系数（默认 0.02，越小越倾向换新主题）
  SHOW_HINTS         选填：是否附带「记忆提示」（默认 1 开启，设 0 关闭）
  FORCE_SEND         选填：设 1 则无视时间窗口与节假日立刻发送（手动测试用）
  FORCE_TARGET       选填：只发指定群（手动测试用，配合 FORCE_SEND=1）

GitHub Actions workflow 配置：
  每个 cron 表达式对应一个群的北京时间推送时刻：
    LSBG-OBD海外业务部  北京 09:00 → UTC 01:00 → '0 1 * * *'
    海外英语能力提升      北京 11:30 → UTC 03:30 → '30 3 * * *'
    海外设计            北京 17:30 → UTC 09:30 → '30 9 * * *'
  代码层再守门确认时刻匹配，防止 cron 延迟导致误发。

同包资源：
  topics.json   主题化词库（职场 / 生活 / 出差 / 海外 / 电力营销 / 电力采集）
  vocab.json    通用 CET-4/6 词库 —— 仅在 topics.json 缺失时回退使用
  holidays.json 法定节假日与调休上班日 —— 每年国务院公布次年安排后更新即可
"""
import hashlib
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import dictionary
import dingtalk
import message

WORDS_PER_DAY = int(os.environ.get("WORDS_PER_DAY", "5"))
DIVERSITY_DECAY = float(os.environ.get("DIVERSITY_DECAY", "0.02"))
SHOW_HINTS = os.environ.get("SHOW_HINTS", "1") not in ("0", "false", "False", "no", "")
FORCE_SEND = os.environ.get("FORCE_SEND", "0") not in ("0", "false", "False", "no", "")
FORCE_TARGET = os.environ.get("FORCE_TARGET", "").strip()

# ── 推送时刻守门（北京时间）──────────────────────────────────
PUSH_TZ_OFFSET = int(os.environ.get("PUSH_TZ_OFFSET", "8"))      # 北京时间 = UTC+8
PUSH_TOLERANCE_MIN = int(os.environ.get("PUSH_TOLERANCE_MIN", "10"))

_HERE = os.path.dirname(os.path.abspath(__file__))
TOPICS_FILE = os.path.join(_HERE, "topics.json")
LEGACY_VOCAB_FILE = os.path.join(_HERE, "vocab.json")
HOLIDAYS_FILE = os.path.join(_HERE, "holidays.json")


def _beijing_now():
    """当前北京时间（naive datetime，不含时区信息）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=PUSH_TZ_OFFSET)


def _load_targets():
    """加载推送目标群列表（JSON 数组）。

    GitHub Actions 通过 Secrets 注入 TARGETS 环境变量。
    返回 [{"name","webhook","secret","hour","minute"}, ...]
    """
    raw = os.environ.get("TARGETS", "").strip()
    if not raw:
        print("[error] TARGETS 环境变量为空")
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        out = []
        for i, t in enumerate(data or []):
            if not isinstance(t, dict):
                continue
            webhook = (t.get("webhook") or "").strip()
            if not webhook:
                continue
            out.append({
                "name": (t.get("name") or "群%d" % (i + 1)).strip(),
                "webhook": webhook,
                "secret": (t.get("secret") or "").strip(),
                "hour": int(t.get("hour", 11)),
                "minute": int(t.get("minute", 30)),
            })
        return out
    except Exception as e:
        print("[error] TARGETS 解析失败: %s" % e)
        return []


def _in_window(now_bj, hour, minute):
    """是否落在某群的推送窗口 [hour:minute, +PUSH_TOLERANCE_MIN)。

    GitHub Actions 的 cron 可能延迟 5-15 分钟触发，
    所以留 10 分钟容错窗口。
    """
    if now_bj.hour != hour:
        return False
    return minute <= now_bj.minute < minute + PUSH_TOLERANCE_MIN


def _load_holidays():
    """读取节假日配置 {年份: {"holidays":[...], "workdays":[...]}}，失败返回空。"""
    try:
        with open(HOLIDAYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as e:
        print("[warn] 读取 holidays.json 失败，仅按周末判断: %s" % e)
        return {}


def _is_workday(day):
    """判断某天是否应该推送。

    规则（按顺序判定）：
      1. 调休上班日（名义周末但实际要上班）→ 发
      2. 法定节假日 → 不发
      3. 周六 / 周日 → 不发
      4. 其余 → 发
    """
    key = day.strftime("%Y-%m-%d")
    year = str(day.year)
    table = _load_holidays().get(year, {})

    for w in table.get("workdays", []) or []:
        if w.get("date") == key:
            return True, "调休上班日"

    for h in table.get("holidays", []) or []:
        if h.get("from", "") <= key <= h.get("to", ""):
            return False, "法定节假日（%s）" % h.get("name", "")

    if day.weekday() >= 5:
        return False, "周末"

    return True, "工作日"


def _load_topics():
    """加载主题化词库 topics.json。"""
    if os.path.exists(TOPICS_FILE):
        with open(TOPICS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        topics = data.get("topics") if isinstance(data, dict) else None
        if topics and isinstance(topics, dict) and len(topics) >= 2:
            cleaned = {t: list(w) for t, w in topics.items() if isinstance(w, list) and w}
            if cleaned:
                order = data.get("_order") if isinstance(data.get("_order"), list) else list(cleaned.keys())
                order = [t for t in order if t in cleaned]
                for t in cleaned:
                    if t not in order:
                        order.append(t)
                labels = data.get("_labels") if isinstance(data.get("_labels"), dict) else {}
                return cleaned, order, labels
    if os.path.exists(LEGACY_VOCAB_FILE):
        with open(LEGACY_VOCAB_FILE, "r", encoding="utf-8") as f:
            words = json.load(f)
        return {"通用": words}, ["通用"], {}
    raise RuntimeError("未找到 topics.json 或 vocab.json")


def _weighted_choice(rnd, pairs):
    """pairs: [(value, weight), ...] 按权重随机取一个 value。"""
    total = sum(w for _, w in pairs)
    if total <= 0:
        return pairs[0][0] if pairs else None
    r = rnd.random() * total
    acc = 0.0
    for v, w in pairs:
        acc += w
        if r <= acc:
            return v
    return pairs[-1][0]


def _pick_today_words(topics, order, labels=None):
    """按展示主题加权抽取今日单词，输出严格按 order 顺序排列。"""
    today = _beijing_now().strftime("%Y-%m-%d")
    seed = int(hashlib.sha256(today.encode("utf-8")).hexdigest()[:16], 16)
    rnd = random.Random(seed)
    labels = labels or {}

    groups = {}
    for t in order:
        groups.setdefault(labels.get(t, t), []).append(t)

    result = []
    used_words = set()
    used_count = {}

    for _ in range(WORDS_PER_DAY):
        cand = []
        for lab, ts in groups.items():
            size = sum(len(topics[t]) for t in ts)
            if size <= 0:
                continue
            w = size * (DIVERSITY_DECAY ** used_count.get(lab, 0))
            if w > 0:
                cand.append((lab, w))
        if not cand:
            break
        lab = _weighted_choice(rnd, cand)

        ts = [t for t in groups[lab] if any(w not in used_words for w in topics[t])]
        if not ts:
            used_count[lab] = used_count.get(lab, 0) + 99
            continue
        topic = _weighted_choice(rnd, [(t, len(topics[t])) for t in ts])
        avail = [w for w in topics[topic] if w not in used_words]
        if not avail:
            continue
        word = rnd.choice(avail)

        used_words.add(word)
        used_count[lab] = used_count.get(lab, 0) + 1
        result.append((topic, word))

    result.sort(key=lambda x: order.index(x[0]))
    return result


def _lookup_with_fallback(picks, topics, labels=None):
    """picks: [(topic, word), ...]，返回 entries，失败词用同主题其他词替换。"""
    labels = labels or {}
    pools = {t: list(words) for t, words in topics.items()}
    rnd = random.Random()

    entries = []
    for topic, word in picks:
        entry = dictionary.fetch_entry(word)
        used = {word}
        retries = 0
        while (not entry or (not entry["senses"] and not entry["example_en"])) and retries < 5:
            retries += 1
            pool = [w for w in pools[topic] if w not in used]
            if not pool:
                break
            replacement = rnd.choice(pool)
            used.add(replacement)
            entry = dictionary.fetch_entry(replacement)
            word = replacement
        if entry and (entry["senses"] or entry["example_en"]):
            entry["topic"] = labels.get(topic, topic)
            entries.append(entry)
    return entries


def run():
    """主入口：GitHub Actions 调用。"""
    now_bj = _beijing_now()

    targets = _load_targets()
    if not targets:
        print("[error] 未配置任何推送目标")
        return 1

    base = {
        "beijing_time": now_bj.strftime("%Y-%m-%d %H:%M"),
        "weekday": "一二三四五六日"[now_bj.weekday()],
    }

    # ── 闸门 1：工作日判断 ────────────────────────────────────
    is_work, day_type = _is_workday(now_bj.date())
    if not FORCE_SEND and not is_work:
        base.update({"ok": True, "sent": False, "skipped": True, "reason": "non-workday", "day_type": day_type})
        print(json.dumps(base, ensure_ascii=False))
        return 0

    # ── 闸门 2：逐个群判断其推送时刻 ──────────────────────────
    due = []
    for t in targets:
        if FORCE_TARGET and t["name"] != FORCE_TARGET:
            continue
        if FORCE_SEND or _in_window(now_bj, t["hour"], t["minute"]):
            due.append(t)

    if not due:
        base.update({
            "ok": True, "sent": False, "skipped": True,
            "reason": "no-target-due", "day_type": day_type,
            "targets": [{"name": t["name"], "at": "%02d:%02d" % (t["hour"], t["minute"])} for t in targets],
        })
        print(json.dumps(base, ensure_ascii=False))
        return 0

    # ── 抽词：只抽一次，多群共用，保证当天各群内容完全一致 ────
    topics, order, labels = _load_topics()
    picks = _pick_today_words(topics, order, labels)
    entries = _lookup_with_fallback(picks, topics, labels)

    title = f"今日 {len(entries)} 词 · English Claw"
    md = message.build(entries, date_str=now_bj.strftime("%Y-%m-%d"), show_hints=SHOW_HINTS)

    # ── 逐群发送 ──────────────────────────────────────────────
    sent_to, failed = [], []
    for t in due:
        ok = dingtalk.send(t["webhook"], t["secret"], title, md)
        (sent_to if ok else failed).append(t["name"])
        print("[send] %s -> %s" % (t["name"], "ok" if ok else "FAILED"))

    base.update({
        "ok": not failed,
        "sent": bool(sent_to),
        "day_type": day_type,
        "sent_to": sent_to,
        "failed_to": failed,
        "words": [{"topic": e["topic"], "word": e["word"]} for e in entries],
    })
    print(json.dumps(base, ensure_ascii=False))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
