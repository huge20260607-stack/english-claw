"""Markdown 消息格式化：把每天的 N 个单词拼成钉钉 Markdown 卡片（带主题标签）。"""
from datetime import datetime


def _phonetic_str(entry):
    parts = []
    if entry.get("usphone"):
        parts.append(f"美 /{entry['usphone']}/")
    if entry.get("ukphone") and entry["ukphone"] != entry.get("usphone"):
        parts.append(f"英 /{entry['ukphone']}/")
    return "  ".join(parts) or "/—/"


def _sense_str(entry):
    lines = []
    for pos, defn in entry.get("senses", []):
        if pos:
            lines.append(f"> **{pos}** {defn}")
        else:
            lines.append(f"> {defn}")
    return "\n".join(lines) if lines else "> (暂无释义)"


def _hint_str(entry):
    """记忆提示块。词根 / 同根词 / 近义词三层独立填充，没有任何素材时整块不渲染。"""
    h = entry.get("hints") or {}
    if not h:
        return ""
    lines = []
    if h.get("root"):
        label = h.get("root_label") or "词根"
        icon = {"词根": "💡", "合成": "🧩"}.get(label, "📜")
        lines.append(f"> {icon} **{label}** {h['root']}")
    if h.get("cognates"):
        lines.append("> 🔗 **同根** " + " · ".join(h["cognates"]))
    if h.get("synonyms"):
        lines.append("> 🔁 **近义** " + " · ".join(h["synonyms"]))
    return "\n".join(lines)


def build(entries, date_str=None, show_hints=True):
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    if not entries:
        return f"### 今日单词\n\n今天 {date_str} 暂无可用释义，请稍后重试。"

    lines = [f"### 🌅 今日单词 · {date_str}", ""]

    # 统计今天覆盖了哪些主题
    topics_today = []
    for e in entries:
        t = e.get("topic")
        if t and t not in topics_today:
            topics_today.append(t)
    if topics_today:
        lines.append(f"**今日主题：** {' · '.join(topics_today)}")
        lines.append("")

    for idx, e in enumerate(entries, 1):
        word = e["word"]
        topic = e.get("topic", "")
        phonetic = _phonetic_str(e)
        senses = _sense_str(e)
        ex_en = e.get("example_en", "") or "(暂无例句)"
        ex_zh = e.get("example_zh", "") or ""

        topic_tag = f"`[{topic}]` " if topic else ""
        lines.append(f"**{idx}. {topic_tag}{word}**  {phonetic}")
        lines.append(senses)
        lines.append("")
        lines.append(f"> 📖 {ex_en}")
        if ex_zh:
            lines.append(f"> 🀄 {ex_zh}")

        if show_hints:
            hint = _hint_str(e)
            if hint:
                lines.append(hint)

        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("📚 *今日份已送达，明天同一时间不见不散～*")
    return "\n".join(lines)